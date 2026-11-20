# Testing

Referenced from `CLAUDE.md` when you are writing or changing tests.

## `unit` vs `integration`

`pyproject.toml` registers five pytest markers:

```toml
markers = [
    "unit: fast tests requiring no external services",
    "integration: tests requiring Postgres or object storage",
    "docker: tests that start containers through the Docker Engine (also marked integration)",
    "slow: a unit test that takes 2 s or more; their number may fall, never rise (scripts/slow-tests-baseline.json)",
    "quarantine(issue): a flaky test, skipped in the required jobs until its issue is fixed",
]
addopts = "-m 'not integration'"
```

Plain `uv run pytest` therefore runs only unit tests by default — `addopts` excludes
anything marked `integration`. CI's `unit` jobs run that same selection in shards (see the next
section), and the integration tier runs in jobs of its own, side by side:
`integration (services, 1/2)` and `(services, 2/2)` run `pytest -m "integration and not docker"`,
`integration (docker)` the `docker` tests (see "Docker isolation tests (M7b)") and
`integration (python assets)` the Python-asset tests with their wheel index. The required check
`integration` passes only when all of them did. They are separate rather than folded into the
unit jobs so a Docker or testcontainers hiccup shows up as its own failure instead of masking
unit results. Run them locally with `uv run pytest -m integration` (that includes the `docker`
tests, which skip without Docker). `slow` and `quarantine` are described in "Time budgets" and
"Flaky tests: the same-day rule"; `uv run pytest -m "not slow"` is the quick local loop.

**Integration tests are for Postgres or object storage, and must use testcontainers against
real services — never mocks.** This is architectural intent: core is
testable with in-memory SQLite and a temp directory, and testcontainers (real Postgres, real
object storage) back the integration tier only. The store is SeaweedFS.
`packages/lakana-core/tests/test_postgres_integration.py` is the worked example: it carries
`pytestmark = pytest.mark.integration` and stands up a real `postgres:16-alpine` via
testcontainers. Everything else runs on in-memory SQLite and `tmp_path`. Add the
`integration` marker and a testcontainers fixture the moment a test needs a real Postgres or
SeaweedFS (S3) instance rather than SQLite/a temp directory; don't reach for mocks of those services
instead.

**Helpers that tests of two packages share live in `scripts/test_support/`** (test code only,
never installed; `scripts` is on every test run's import path). The local HTTPS server with a
throwaway CA and the CONNECT proxy of the egress tests are there (`egress_helpers.py`); import them as
`from scripts.test_support.egress_helpers import ...`. A test that needs a port nothing serves uses
`closed_port()` from `scripts/test_support/ports.py`: it keeps the port bound (not listening) until the
test ends, so a parallel test cannot take it (Linux only: on macOS a connect to a bound, non-listening socket times out instead of being refused, so there it is bind-and-close and a parallel test could in theory take the port, a race accepted); never bind, close and reuse a port number to get one that nothing serves (a port a server is about to listen on is the exception: it has to be free, so it cannot be held). Never add another package's `tests/`
directory to `sys.path`.

**A test may leave a read-only tree** (the code-unpack tests do, on purpose). A session fixture
(`scripts/pytest_writable_tmp.py`, loaded by both `conftest.py` files) makes everything below
pytest's base temp directory writable at the end, so pytest can rotate `/tmp/pytest-of-$USER`.
After a run that was killed, `chmod -R u+w /tmp/pytest-of-$USER` does the same by hand.

**Only a failed test keeps its `tmp_path`** (`tmp_path_retention_policy = "failed"` in
`pyproject.toml`): pytest removes a passed test's directory at its teardown. `/tmp` is a RAM disk
and its files count against the scope's memory cap, so kept for every test they grew to about 1 GB
in a run over all packages at `-n 3`; with the workers' 1.3 GB each and the controller's 1.1 GB
that ran into a 6G cap (the kernel's reclaim made the run crawl, then killed a worker). A test that
patches `os.open`, `os.lstat` or a similar function must forward every argument (`dir_fd`
included): the removal runs while a test's `monkeypatch` may still be in place.

**Git providers are emulated in the unit tier (M5b).** `packages/lakana-worker/tests/worker_git_helpers.py`
serves bare repositories with `git http-backend` behind an in-process HTTPS server (self-signed
certificate, basic auth; any-username, fixed-username and 401/403/404 modes; protocol v2 or v0 for
`allowReachableSHA1InWant`). It needs only the `git` binary, so the fetcher and the deploy pipeline
count toward the coverage ratchet. SSH needs a real `sshd` and runs in the integration tier
(`test_git_ssh_integration.py`, built from `tests/sshd/Dockerfile`). A live test against github.com
and gitlab.com is #16. A repository owned by *another uid* (the Compose mount) cannot be made
in a unit test without root; `scripts/compose_smoke_test.sh` pins it end to end.

## Running the unit suite in parallel

CI runs the unit suite in **four shards per Python version**, side by side: the jobs
`unit (3.11, 1/4)` to `unit (3.14, 4/4)`. `LAKANA_TEST_SHARD=I/N` (read by
`scripts/pytest_selection.py`, which both `conftest.py` files load) keeps shard I of N: whole test
files, spread over the shards by the seconds in `scripts/test-durations.json`, the longest first,
each to the shard with the least time so far; a file without an entry counts one second. Every
shard and every xdist worker computes the same split from the same collection, so the shards
together run every test once. When the shards drift apart (a budget failure shows it), refresh the
file from the junit artifacts of a green run (`junit-unit-*`, `junit-integration-*`); a test checks that at most a tenth of the test files lack an entry, because a file without one counts one second and a stale file skews the shards (in November 2026 a quarter had none, and the three shards summed to 1082, 1429 and 1396 s of measured test time; with a fresh file they sum to 1302 s each). The macOS run and the Linux run both use four shards: three shards of balanced files still took 13 to 14 minutes each (on Linux 3.14, 9 to 16 minutes in the ten runs before November 13, 2026, with up to 6 minutes between runs of the same shard), too close to the 15-minute budget. Each coverage shard uploads `.coverage.shard-N`; `test (3.14)` downloads them all by the `coverage-data-*` pattern, so a further shard needs no change there.

```bash
gh run download RUN_ID -p 'junit-*' -D ~/.cache/lakana-junit
uv run python scripts/time_budgets.py durations --output scripts/test-durations.json ~/.cache/lakana-junit/*/*.xml
```

Within a shard the run is parallel too: the 3.11 shards use `-n auto --maxprocesses 3 --dist
loadfile` (pytest-xdist). **The 3.14 shards, which measure coverage, use `--maxprocesses 2`**:
they need more memory per process (below). The required checks `test (3.11)` and `test (3.14)`
wait for the shards and pass only when every shard did (`needs.unit.result` covers both versions,
so a failing shard fails both checks; the shard's own job names the test). `test (3.14)` also
downloads each shard's coverage data, runs `coverage combine`, `coverage json` and `coverage report
--skip-covered --show-missing`, then the coverage ratchet and the slow-test ratchet (see "Time
budgets").

`addopts` carries no `-n`, so a plain `uv run pytest` stays serial. The default Python of a
checkout is 3.14 (`.python-version`; the image runs 3.14 too), so a plain `uv run` is the 3.14
run. Locally, a pull request runs the tests its change selects (see "Local gates"); to run the
whole suite with coverage the way the 3.14 shards do together, run it **one package at a time**
and combine the data, as CI combines its shards:

```bash
failed=
for target in packages/lakana-core packages/lakana packages/lakana-server \
    packages/lakana-worker scripts; do
  COVERAGE_FILE=".coverage.local-$(basename "$target")" \
    systemd-run --user --scope -q -p MemoryMax=8G -p MemorySwapMax=0 timeout 3600 \
    uv run pytest "$target" -n 2 --dist loadfile --cov --cov-report= -q -p no:cacheprovider \
    || { failed="$target"; break; }
done
if [ -n "$failed" ]; then
  echo "Stopped at $failed: fix it first; coverage of a partial run would read as a drop."
else
  uv run coverage combine .coverage.local-* && uv run coverage json \
    && uv run python scripts/coverage_ratchet.py
fi
```

One run over the whole repository no longer fits (#488): it grew to 7.0 GB by about 99 % (in
`scripts/tests/test_workflows.py`) and the memory limit stopped it. Its two workers live through
all five targets; which part of what they keep grows (coverage data, a cache, imported modules)
was not isolated, since the per-target runs fit with room to spare. Fresh workers per target
peak at 2.9 GB (`lakana-core`), 2.3 GB (`lakana-worker`), 2.2 GB (`lakana-server`, `scripts`)
and 1.3 GB (`lakana`) (2026-11-14, Python 3.14, `-n 2`, `--cov`); the combined report is the
whole suite's and passed the ratchet at 100.00 %. `lakana-core` took 39 minutes that day on a
busy machine, `lakana-server` and `lakana-worker` 7 each, `scripts` 3, `lakana` 1.5.

- **Why `loadfile`:** every test of one file runs in one process, in file order. Test-level
  isolation under `--dist load` (module-level state, ports, environment variables,
  `monkeypatch` leftovers) is unproven; do not switch without running the suite that way
  repeatedly.
- **A `parametrize` value must be the same in every process.** Each worker collects the tests
  itself and xdist refuses a run whose workers collected different ids, so `uuid4()`, `random`,
  the clock or the process id in a `parametrize` argument fails collection (only in
  parallel). Use a fixed literal, or fixed `ids=[...]`.
  `scripts/tests/test_parametrize_values_are_stable.py` reads every test file and names file
  and line (it does not see a value built elsewhere and passed by name, `metafunc.parametrize`
  or fixture `params=`).
- **A worker's environment is not the serial one.** On Python 3.14 a `pytest -n` worker has
  `LINES=24` in `os.environ` and a serial run does not; rich then sizes a console differently.
  A test that depends on a console's size gives rich both width and height.
- **Memory, Python 3.11 (and 3.13):** only the controller runs the watchdog (see "Memory
  limit"); it counts the workers. At `-n 4` CI's peak reached 5,956-6,100 MB in October 2026,
  within 200 MB of the 6 GB limit, and a pull request crossed it; so keep `MemoryMax=6G` and no
  more than three workers.
- **Memory, Python 3.14: at most two workers, and `core = "ctrace"` stays.** On 3.14
  coverage.py's default core is `sysmon`, which keeps every code object the suite compiles
  alive. The CLI tests compiled about 1.7 million throwaway snippets (Typer resolves type hints
  of a freshly built app on every `invoke`; since #101 `tests/cli/conftest.py` builds one app per
  process), so the serial run peaked at 4.8 GB and no parallel run fitted (2026-10-10: `-n 2`
  killed under 6 GB, `-n 4` under 6 and 8 GB).
  `[tool.coverage.run] core = "ctrace"` in `pyproject.toml` selects the C tracer that 3.11 to
  3.13 use anyway. With it, measured 2026-10-11 under a 6 GB cap, coverage 100 % each time:
  serial 797 s / 1.9 GB; `-n 2` 397 s / 4.2 GB; `-n 3` 274 s / 5.0 GB; `-n 4` killed at 96 %.
  The 3.14 processes are still heavier than those of 3.11 and 3.13, so CI uses two, and so does a local run.
  If a parallel 3.14 run is killed again, check first that the setting is still there.
- **The cap can end a run before the watchdog does.** The watchdog counts resident anonymous
  and shared pages; a cgroup cap also counts page cache and the files in `/tmp` (a RAM disk,
  about 0.6 GB at the end of the suite). Under a cap the kernel can therefore kill the run
  first, even with `LAKANA_TEST_MEMORY_LIMIT_MB` set below the cap: systemd reports
  `oom-kill` and the exit status is 143. A `timeout` ends with 124; tell the two apart with
  `journalctl --user | grep oom-kill`.
- **Measured 2026-10-10, Python 3.13, this machine:** serial 533 s without coverage;
  `-n 4 --dist loadfile` 139 s without and 185 s with `--cov --cov-report=json`; 10,818 tests
  passed, coverage 100 %, and `scripts/coverage_ratchet.py` accepts the merged report. Python
  3.11 at `-n 4`: 154 s, peak 4.77 GB.

### The CLI tests share one app (#101)

`invoke` in `packages/lakana/tests/cli/conftest.py` runs the CLI in-process. Typer turns an app
into a click command, and resolves the type hints of every command while it does, on every
call; with `from __future__ import annotations` each resolution compiles a snippet. `invoke`
therefore builds the app and its command once per process (`functools.cache`) and hands the
command to Typer's runner. Nothing reads state from the app between invocations: the state
comes in through `obj`, the environment through `env`, and a command looks up what a test
patches in its module when it runs. A test that needs an app of its own (a changed registrar,
as `test_build_app_calls_every_registrar`) calls `build_app()` and runs it with its own
`CliRunner`; none of the 2,343 tests needed that for `invoke`.

Measured 2026-10-19, `packages/lakana/tests` alone, serial, in a 6 GB scope, with
`/usr/bin/time -v` inside it (2,343 passed, 1 skipped). "Peak" is the `test memory` line of
the run; "largest process" is `Maximum resident set size` of `time -v`:

| Python | Coverage | Before: time | peak | largest process | After: time | peak | largest process |
|---|---|---|---|---|---|---|---|
| 3.14 | none | 58.5 s | 223 MB | 278 MB | 12.8 s | 205 MB | 263 MB |
| 3.14 | `--cov` (ctrace) | 87.7 s | 229 MB | 298 MB | 17.4 s | 208 MB | 279 MB |
| 3.13 | none | 58.0 s | 179 MB | 251 MB | 12.6 s | 168 MB | 233 MB |
| 3.13 | `--cov` | 85.3 s | 178 MB | 247 MB | 17.3 s | 174 MB | 244 MB |

The time falls to a fifth on both versions. The memory of this package alone was already low
since `core = "ctrace"` (see "Memory, Python 3.14" above) and falls by about 5 %; the 3.14
processes stay heavier than those of 3.13 by about 20 to 30 MB here.

The CLI tests against the real app do the same: `run_cli` in
`packages/lakana-server/tests/cli/conftest.py` (what the `invoke_cli` fixture returns) builds the
command once, when the conftest is imported, and the CI OIDC how-to and Postgres CLI tests call it
too (#722). Each conversion had cost about 0.13 s under coverage. Measured 2026-11-18, serial,
under `--cov`: the grant-twice test went from 1.60 s to 0.69 s alone (2.8–3.0 s to 1.2–1.5 s with
every core busy), and `packages/lakana-server/tests/cli` without the integration tests from
27.3 s to 12.1 s at `-n 4`. Both runners take their environment from
`scripts/test_support/cli_env.py`, which clears the CLI's own variables and GitHub Actions' OIDC
request variables and asks for plain output.

### The server and worker tests copy one catalog and skip fsync (#327)

Every server test still gets its own SQLite file and its own app; two costs are shared instead.

- **The schema is created once per process.** `catalog_template` (session scope, in
  `packages/lakana-server/tests/conftest.py`) runs `Base.metadata.create_all` into a read-only
  file; `settings` copies it into the test's `tmp_path` (under a millisecond, against about
  0.7 s for the schema). A test that builds its own `Settings` with a fresh file still pays the
  0.7 s; copy `catalog_template` instead when there are many of them.
- **SQLite commits do not wait for the disk.** `_sqlite_commits_without_fsync` registers a
  `connect` listener on every engine of the server test process that sets
  `PRAGMA synchronous=OFF`. No unit test can observe durability across a power cut; WAL,
  foreign keys and the busy timeout still come from `lakana_core.db`. Postgres connections
  are left alone.
- `tests/test_test_hygiene.py` pins both: each test's copy has the full schema and sees no
  other test's write (in either order), the template is read-only, and the production pragmas
  are still in force.
- **The worker tests do the same.** `packages/lakana-worker/tests/conftest.py` has its own
  `_sqlite_commits_without_fsync` and `catalog_template`, and `catalog_database` hands a test
  its copy; the `world` fixture and `py` (`test_python_tasks.py`) build on it.
  `tests/test_test_databases.py` pins it as `test_test_hygiene.py` does for the server. Two
  set-back tests of `test_python_tasks.py` that the slow-test ratchet flagged went from 2.0–3.0 s
  to 0.7–1.0 s on their own (2026-10-30, Python 3.14, `--cov`, `--basetemp` on disk).

Measured 2026-10-24, `packages/lakana-server` alone, Python 3.14, `-n 2 --dist loadfile`, no
coverage, 6 GB scope, `--basetemp` on disk: 1,099 s before, 116 s after; the 902 tests under
`tests/api/` from 1.99 s to 0.17 s each on average (setup and teardown included). Random order
(`uv run --with pytest-randomly`, two seeds) passes too.

Almost all of that was waiting for the disk: the schema takes about 1 s with `fsync` on that disk
and 14 ms without. Where `fsync` is cheap (a `tmpfs` such as `/tmp` on this machine, or CI's
runners) the gain is small: CI's unit steps took as long after the change as before (3.14 with
coverage 19.3 against 18.4 min, 3.11 15.2 against 13.9 min, within run-to-run noise). The
change pays off for a `--basetemp` on a real disk, as the agents' capped runs use.

## Pull requests that change documentation only

A pull request that changes nothing but documentation skips the heavy jobs: `lint`, the `unit`
shards, `integration` and its jobs, `five-minute`, `compose-smoke`, `docs-examples` and its shards, the macOS
jobs, `quarantined tests` and `static`. The `test` job still
runs on both Python versions, but only `scripts/tests`, which holds every test that reads the
documentation (the legacy-name check among them). `docs` (the block check, the strict build and the
check of the built site), `changes`, `conventional`, `secrets`, `cla` and `time budgets` run as always. For a
documentation-only change the `docs` job and the `test` job's `scripts/tests` together are the
guard against a broken page, so `docs` has no condition. `scripts/tests/test_docs_site.py`,
which reads `mkdocs.yml` (the navigation, `docs_dir`), is among those `scripts/tests`, so a
docs-only change to the navigation is checked by it and by the strict build. Because `lint` and `static` are skipped for such a change,
the same file fails on a `hooks:` key or a plugin in `mkdocs.yml` (code run at build time) until it
is listed in a pull request that is not docs-only. The same file refuses a Markdown extension outside
`ALLOWED_EXTENSIONS`. A push to `main` runs everything unless
it proves the content was tested (below).

"Documentation" is defined in one place, `scripts/docs_only.py`: a file under `docs/`, the root
`mkdocs.yml` (the site's navigation: a new page needs an entry), or a
Markdown file that is not inside a package's `src` tree. **`docs/examples/` counts as code**,
Markdown files included, so that editing an example runs it. Anything else, including a workflow
file, `LICENSE`, `NOTICE` and an `mkdocs.yml` anywhere but the root, runs the whole pipeline. So does every doubt: an empty or very
long file list, or a failure of the detection itself.

Two things keep this from weakening the required checks:

- The `changes` job takes `scripts/docs_only.py` from the base branch, so a pull request
  cannot change the rule it is judged by. A change to the rule takes effect for the pull
  requests after it.
- `test (3.11)` and `test (3.14)` are required checks, and a skipped matrix job would never
  report under those names. The `test` job therefore always runs and switches its steps: the
  documentation tests for a docs-only change, otherwise the check of the `unit` shards (the
  one thing that does change its matrix is a draft, below).

A test that reads a documentation file belongs in `scripts/tests`. Placed anywhere else, it
does not run for a docs-only pull request and would fail on `main` after the merge.
`scripts/tests/test_docs_only.py` pins the rule and the workflow conditions.

## What CI runs, and when

`ci.yml` has one `changes` job that decides what the rest runs. Every doubt, every error and
every missing output of it means "run everything".

| Event | Runs |
|---|---|
| Pull request, **draft** | `lint`, `ui`, `docs`, `docs-examples` (with its four shards), the 3.14 `unit` shards and `test (3.14)` (with coverage and the ratchets), `quarantined tests`, `time budgets` |
| Pull request, **ready for review** (also the moment it leaves draft) | everything (`ui` included): the above plus the 3.11 shards and `test (3.11)`, the macOS shards and `test (macos)` (they test only if the pull request touches `packages/`), the `integration` jobs, `five-minute`, `compose-smoke` |
| Dependabot's pull requests | everything (they are never drafts) |
| Documentation-only pull request | see the section above; a draft also lacks `test (3.11)` |
| Push to `main`: the squash merge of an up-to-date pull request whose last run was complete | `lint`, `docs` (the rest was just tested on the same tree), the macOS jobs, `time budgets` |
| Push to `main`: anything else (a direct push, a draft's last run, a re-run that failed, any doubt) | everything |

- **Drafts.** A draft skips the `integration` jobs, `five-minute`, `compose-smoke`, the macOS
  jobs and the 3.11 legs of `unit` and `test`. The 3.11 legs are not skipped by a condition: the
  `unit` and `test` matrices are built from the `changes` job's `python_versions` output (`["3.14"]` on a draft, both versions otherwise), so
  on a draft the required check `test (3.11)` does not exist, which blocks a merge. A job
  skipped by a condition on a matrix cell would show as a green check that ran nothing.
  `ready_for_review` is a trigger type of the workflow (it is not a default), so leaving draft
  starts the full run. A draft cannot be merged anyway.
- **The merge to `main`.** After a squash merge of an up-to-date pull request, the tree on `main`
  is the tree of the pull request's head commit, which CI has just tested. On a push to `main`
  the `changes` job runs `scripts/ci_already_tested.py` (fetched from the pushed commit), and if
  it proves that, `unit`, `test`, `ui`, the `integration` jobs, `five-minute`, `compose-smoke`,
  `docs-examples` (with its shards) and `quarantined tests` skip. It says "already tested" only if the commit has exactly one pull request, merged with
  this very commit into the repository's default branch, whose base was never changed (no
  `base_ref_changed` or `base_ref_force_pushed` event: a run made against another branch
  proves nothing about `main`) and whose head commit no other pull request shares (runs are
  found by head commit, so another pull request's run would be taken for this one's), one
  parent that the head contains, the head's tree equals this tree, and
  the latest `pull_request` run of `ci.yml` for the head completed with `test (3.11)`,
  `test (3.14)`, `integration`, `five-minute`, `compose-smoke`, `docs-examples` and `ui` all
  `success` (a `skipped` job, as on a draft or a documentation-only change, is a no; "latest"
  is by run number, so a failed re-run of an older run does not override a newer green run).
  The first lookup of the pull request is retried once after 10 s, for the API's lag right after
  a merge. Every
  doubt, API error, timeout, missing `gh`, or truncated page says "not tested" and runs
  everything; the job log has one line with the reason. A pull request event never skips this
  way. If you rename one of the seven jobs, change `REQUIRED_JOBS` in the script too: until then
  it says "not tested" (safe, but the saving is gone). The job has `actions: read` and
  `pull-requests: read` for this.
- **Agents keep their pull requests in draft while they iterate** (`gh pr create --draft`) and
  mark them ready (`gh pr ready`) when the work is done, which starts the full run once instead
  of on every push.
- **The integration jobs** run side by side, so each failure is named by its own job and none
  waits for another: `integration (services, 1/2)` and `(services, 2/2)` run the integration tests
  that install nothing and start no task container (`-m "integration and not docker"`, sharded
  like the unit suite, each writing a junit report), `integration (docker)` the "Docker isolation
  tests" (`-m docker` with `LAKANA_TEST_DOCKER=1`, see "Docker isolation tests (M7b)"), and
  `integration (python assets)` builds the wheel index (only it needs one) and runs the
  Python-asset tests. The required check `integration` waits for the three and fails naming each
  one that did not succeed. Each has a 30-minute timeout.
- **`compose-smoke`** starts the real console scripts in containers, so it runs for any change under
  `packages/*/src/` as well as for the bundle, packaging and script files in the filter of its
  "Did the bundle change?" step, and for the files and the REST stand-in of the `ingest-files`
  and `ingest-rest` examples, which its ingestion part reads (M6's exit criterion, see
  `docs/agent/release.md`). It also runs the backup and restore drill (#389,
  `docs/examples/compose-restore.sh`, which the how-to "Deploying with Compose" shows): back up the
  catalog and the storage, change a table, lose the instance, restore into a new directory, and
  compare rows, snapshot id, runs, grants, secrets, an artifact and a table of a second environment;
  it prints `restore took N s` (the RTO). `scripts/tests/test_compose_restore_drill.py` pins what the
  script must check. Its last step, `scripts/offline_install_test.sh`, runs the built image under
  `docker run --network none` with `LAKANA_WORKER__EXTENSION_SOURCE=none` and checks that local mode
  starts and answers `lakana sql` (#493, the proof for the offline install how-to); the script is in
  the filter too. A `unit` shard has a 25-minute timeout, `test` 15 minutes.
- **Coverage** is measured on 3.14 only, by the three 3.14 shards, and combined in exactly one
  job, `test (3.14)` (see the coverage ratchet below). That job uploads the combined report,
  `coverage.json`, as the artifact `coverage-report` (kept 7 days), also when the ratchet fails,
  and its log lists every file below 100 % with the missed lines.
- **Time budgets** are checked by `time budgets`, the last job, and by the slow-test ratchet in
  `test (3.14)` (see "Time budgets" below).
- **`quarantined tests`** runs the tests in quarantine and never fails the run (see "Flaky tests:
  the same-day rule").
- **Duplication** is measured by the `lint` job: `jscpd`, pinned in `tools/jscpd`, scans
  `packages/*/src` and `ui/src` (not the tests) and `scripts/duplication_ratchet.py` fails when
  the duplicated lines or the clones rise (see "The duplication ratchet" below).
- **`ui`** is the required check of the web interface: it waits for four jobs that run side by
  side, `ui (gates)` (every gate but the browser tests and `prove-gates`), `ui (gate proofs)`
  (`prove-gates`, on a build of its own), `ui (e2e smoke, N/2)` and `ui (e2e server)` (one
  Playwright suite each, on a build of their own; the smoke suite in two shards,
  `playwright test --shard=N/2`, since #714 took one job over its 10 minutes), and is green only
  when all four were (see "The `ui/` gates" below; split in #586 because one job ran over its
  10 minutes, and the proofs split off `ui (gates)` when the two together took up to
  10.3 minutes).
- **`test (macos)`** (job `test-macos`) is the macOS check: Lakana's local mode supports macOS
  (Python 3.13 and later), and the four shards it collects, `unit (macos, 1/4)` to `(macos, 4/4)`
  (job `unit-macos`), run what a macOS user runs, on GitHub's `macos-latest`
  (Apple Silicon, 3 cores, 7 GB) with Python 3.14. They run the `packages/` unit suite (not
  `scripts/`, which is Linux tooling whose tests need GNU tools) with
  `pytest packages -m "not integration" -n 2`, without coverage (the ratchet stays on the Linux 3.14 job), and
  the fourth shard runs `scripts/run_docs_examples.sh getting-started`, which ends with `lakana server reset --local`.
  Docker-based tests (`integration`, `five-minute`, `compose-smoke`) do not run: macOS runners
  have no Docker. `scripts/pytest_memory_limit.py` needs `/proc`, so on macOS it does nothing and
  the limit's 6 GB default (below the runner's 7 GB) is not enforced; two workers fit. The example
  runner needs GNU `timeout` and `setsid` and bash 4, so the job installs Homebrew's `bash` and
  `coreutils` and puts a small Python `setsid` in front of the path. The shards always run on
  GitHub's runner, never the `CI_RUNNER` switch (see "CI runners"); a pull request runs them only if
  it changes `packages/`, the root `pyproject.toml`, `uv.lock`, `docs/examples/` or the files the job runs, and it skips a
  draft and a documentation-only change like the other heavy jobs. A failure fails the run (the
  first runs' 50 failures were fixed in #153). It is **not a required check yet**; the maintainer
  makes it one after a week of green runs (#148). A failure that only macOS shows is fixed or
  filed as an issue. A test that cannot hold on macOS (it reads `/proc`, or needs a file name
  APFS refuses) is skipped there with `skipif(sys.platform == "darwin", reason=...)` naming why;
  one that only assumes a Linux path or limit is made portable instead. Code that differs on macOS
  gets a seam (a missing `os.O_PATH`, `sys.platform`, an errno) so its macOS branch runs on Linux
  too and coverage holds.

### Scorecard, Semgrep and the release workflow

- **`scorecard.yml`** runs OpenSSF Scorecard on a push to `main`, weekly (Mondays) and when the
  branch protection changes. Workflow permissions are `read-all`; its job adds only
  `security-events: write` (the SARIF upload to code scanning) and `id-token: write` (publishing
  the score that the README badge shows). Its results are at
  <https://scorecard.dev/viewer/?uri=github.com/lakana-labs/lakana>. Findings are fixed one at a
  time, when it reports them, never in bulk. `scripts/tests/test_workflows.py` pins the
  permissions, the triggers and its runner; another test there pins every action of every workflow
  by commit SHA.
- **`canary.yml`** (Dependency canary) runs daily and on demand. It builds the wheels from `main`
  and runs `scripts/five_minute_test.sh` as the `five-minute` job does: `lakana` is pinned to the
  job's own build, every third-party dependency resolves from PyPI unpinned, so it sees a breaking
  release of `sqlglot`, `duckdb`, `pyiceberg` or anything else within a day, which the lock and the
  pull-request jobs cannot. A failure is a failed scheduled run, which `scheduled-failure.yml`
  turns into an issue (the canary job itself has `contents: read` only); the step summary lists the resolved
  versions (`pip freeze`) so the run names the release that broke it. It is outside `ci.yml` and
  its `changes` gating, and on GitHub's runner whatever `CI_RUNNER` says.
- **`security-deep.yml`** runs semgrep and Trivy weekly. The semgrep scan (a pinned image, no write
  scope, no token in the workspace) keeps its SARIF as an artifact; `semgrep-upload` alone has
  `security-events: write` and does nothing but upload it to code scanning. Dependabot does not
  read a workflow's `container:` image: bump the semgrep version and digest by hand.
- **`scheduled-failure.yml`** (Scheduled failure report) runs when a watched workflow completes
  (`workflow_run`) and acts only on a failed *scheduled* run of `canary.yml`, `codeql.yml`,
  `fuzz.yml`, `scorecard.yml` or `security-deep.yml`: GitHub tells only the last editor of a cron about a red
  scheduled run, and the weekly image scan once failed unseen for three weeks. It opens one issue
  per workflow (`Scheduled run failed: <name>`, labelled `type:security`, assigned to the
  maintainer) or comments on the one still open; close it once a scheduled run passes. Its one
  job has `issues: write` and nothing else, checks nothing out and gets the run's fields as
  environment variables. A new scheduled workflow joins its list, or `_NOT_REPORTED` in
  `scripts/tests/test_workflows.py` says why not (`mutation.yml` today).
- **`fuzz.yml`** (Fuzzing) runs ClusterFuzzLite over the Atheris harnesses in `fuzz/`: about five
  minutes on a pull request from a branch of this repository (never a fork's) that changes what
  they load, an hour every night on `main` with corpus pruning after it (`.clusterfuzzlite/nightly.sh`).
  The corpus is kept with `actions/cache`: only the nightly job saves it, a pull request restores
  it read-only. On GitHub's runner whatever `CI_RUNNER` says (the actions run Docker, and a pull
  request's harnesses are its own code); every job has `contents: read`, the nightly job also
  `actions: read` for one `gh cache list` (whether a corpus was kept before), and no
  ClusterFuzzLite action gets a token. What it fuzzes and where a crash goes: "Fuzzing" in
  `docs/agent/security.md`.
- **`codeql.yml`** runs CodeQL for Python on pull requests, on a push to `main` and weekly, on a
  GitHub-hosted runner, with `security-events: write` on its one job. It is CodeQL's *advanced*
  setup: switching on *default setup* in the repository's code-scanning settings would make its
  uploads fail, so leave that toggle off.
- **Token permissions.** No workflow has a write permission at its top level: each job names its
  own (`cla.yml`, `security-deep.yml` and `scheduled-failure.yml` included). `scripts/tests/test_workflows.py` fails on one.
- **`release.yml`** is described in `docs/agent/release.md`; it also attaches the SBOM to each
  GitHub release, and publishes the documentation site on GitHub Pages (`pages` and
  `pages-deploy`; see "The documentation site on GitHub Pages" there).

## The documentation site

The user documentation is a site under `docs/site/`, built by Zensical from `mkdocs.yml`. Every
code block on a page is synced from a tested file, parsed, marked as output, or marked as not
tested and counted (below). Everything here is for someone adding or changing a page.

### Pages

The site has six tabs, in this order. Each is fed by one or two directories, and each but the
first and the last opens on an `index.md` that introduces it and links every page of the tab:

| Tab | Directory | A page is |
|---|---|---|
| What is Lakana? | `docs/site/index.md` | the home page |
| Learn | `docs/site/learn/` and `docs/site/tutorials/` | learning by doing, start to finish, one working result |
| How-to guides | `docs/site/how-to/` | one task, the shortest path |
| Concepts | `docs/site/explanation/` | how Lakana works and why |
| Reference | `docs/site/reference/` | exact facts: every option, default and limit |
| Project | `docs/site/explanation/` | the roadmap, support, upgrade policy and known gaps |

Within a tab, `nav` groups the pages into named sections, and the tab's index has one `##` per
section in the same order. A page keeps its file when it moves between tabs or sections: only
`nav` and the index pages change, so no published URL breaks (the site has no redirects).

The reference files below are generated, never edited by hand. The command line reference comes
from the Typer app: `reference/cli.md` is its index (the options for every command and one line
per page), `reference/cli/commands.md` has the commands outside a group, and `reference/cli/`
has one page per command group, named after the command (`reference/cli/lakana-table.md` for
`lakana table`), so link a command on its group's page, for example
`cli/lakana-table.md#lakana-table-maintain`. Also generated:
`reference/settings.md` from the settings models of `lakana-core` and `lakana-server` (the
description of a variable is its entry in the model's `Parameters` docstring section, so document
a new setting there), and `reference/openapi.json`, the OpenAPI document of the platform REST API
(`docs/agent/release.md`, "The REST API contract"). The REST reference is generated from
`openapi.json`: `reference/rest/<tag>.md` is one page per OpenAPI tag (its routes with their
parameters, request body and answers), and the list of those pages in `reference/rest-api.md` is
the generated region between the `generated:begin rest-pages` and `generated:end rest-pages`
markers; the rest of `rest-api.md` is written by hand. A route's text on those pages is its
summary and description in the OpenAPI document, so write them for a user. The Python SDK
reference, `reference/sdk/`, is generated from the docstrings of the `lakana` package: `index.md`
lists every name of `lakana.__all__` and every resource the client's constructor sets,
`client.md` is the client, `errors.md` the exported errors, one page per module of the exported
functions (`assets.md`) and one per module of `lakana/resources` the client uses
(`reference/sdk/ci-trust-rules.md` for `ci_trust_rules.py`). A method's text there is its
numpydoc docstring (summary, body, `Parameters`, `Returns`/`Yields`, `Raises`), so write it for a
user; `scripts/tests/test_generate_reference_sdk.py` fails on a private `lakana._` name, a
private name in a signature, a parameter without a description, or an error that `lakana` does
not export. The data classes of `lakana._models` are named only as return types. After a
command, an option, a setting, a route or a public SDK docstring changes, run
`uv run python scripts/generate_reference.py` and commit the pages; `--check` fails, naming the
stale page, and runs in CI's `docs` job and in `scripts/tests/test_generate_reference.py`.

The generator also owns the directories `reference/cli/`, `reference/rest/` and `reference/sdk/`:
a page there that the code no longer produces is deleted when it runs, and `--check` reports it
as left over. It owns the `api-nav` region of `mkdocs.yml` too, the `API reference` entries
between the `generated:begin api-nav` and `generated:end api-nav` markers. Never edit inside
`generated:begin`/`generated:end` markers: change the code and regenerate.

List a new page in its tab's section under `nav` in `mkdocs.yml`, and link it from its tab's
index (`test_each_tab_index_links_every_page_of_its_tab`). Every entry must be a page and every
page an entry, once; `scripts/tests/test_docs_site.py` fails otherwise. Link to other pages with
relative `.md` links; the strict build fails on a broken link or anchor.

### Rules for pages

- **Fenced blocks only.** Use triple backticks. The block checker does not see a four-space
  indented code block, and it reports a fence inside a blockquote and an HTML `<pre>`, because
  it cannot judge them.
- **No command in running text.** A command the reader is meant to type goes in a block, so it
  is checked. Inline code is for names (a file, an option, a table).
- **Output blocks show real output.** Run the command and paste what it printed. Do not
  write what you expect. Better, tie the block to the region that prints it
  (`<!-- output: EXAMPLE#REGION -->`, below): CI then compares the two on every change.
- **No milestone names.** A reader does not know `M5c1` or `M7b`; say what the work is or "in a
  later release". Only `explanation/scope.md` and `explanation/roadmap.md`, which define the
  milestones, may use them; `scripts/tests/test_docs_site.py` fails on any other page.
- **No feature that loads from a CDN.** The site must work on an internal network. Do not use
  mermaid diagrams, image zoom, editable or runnable code blocks, `repo_url` (it queries the
  GitHub API), analytics, or a font, image or script from a URL. Two things check this.
  `scripts/tests/test_docs_site.py` asserts the configuration (no `repo_url`, no glightbox,
  `markdown-exec` or mermaid fence, `theme.font` false, no `extra.analytics`, no URL in
  `extra_javascript` or `extra_css`) and that no page contains a mermaid, pyodide or glightbox
  marker. `scripts/check_built_site.py site-build` reads the **built** HTML and CSS: it fails
  on a `script`, `link`, `img`, `iframe`, `source`, `video`, `audio`, `embed` or `object`
  that loads from another host, on a CSS `url()` or `@import` that does, and on a local `src`
  or `href` of those elements whose file is not in the build (a missing image builds clean).
  Plain `<a href>` links to other sites are fine.
- **The look is Still Water, in light and dark.** `theme.palette` in `mkdocs.yml` has a light
  (`default`) and a dark (`slate`) scheme: a first visit follows the system setting, the toggle
  in the header switches and Zensical keeps the choice. `docs/site/assets/stylesheets/still-water.css`
  holds the interface's colour tokens (the values of `ui/src/styles/tokens.css`) per scheme
  and maps them onto Zensical's `--md-*` variables; the reading type sizes and the code colours
  are the project website's, so the two read alike. IBM Plex is served from
  `docs/site/assets/fonts/` (the complete woff2 files from the npm packages, unmodified, with
  `OFL.txt`); the header shows the brand logo, the tab the interface's favicon.
  `scripts/tests/test_docs_look.py` fails when a colour differs from `tokens.css`, when a text
  pair falls below WCAG AA (body text 7:1, other text and code colours 4.5:1) in either scheme,
  when the schemes define different colours, or when a variable or font file is missing. So a
  change to a colour token in `tokens.css` copies the new value into both schemes of the
  stylesheet in the same change. Zensical's search dialog lives in a shadow root and keeps its
  own translucent, blurred panel; it takes our colours, not our panel style.
- **The count of unchecked output blocks only goes down.** An output block is *unchecked* when it
  is plain `<!-- output -->`: no example prints it, so nothing notices when the CLI's output
  changes. The checker prints the number it compares and the number it does not;
  `UNCHECKED_OUTPUT_CEILING` in `scripts/tests/test_docs_site.py` fails the tests when it rises,
  and when it falls without the ceiling (lower it in the same change). Tie every new block that
  an example can print.

- **The count of not-tested blocks only goes down.** The checker prints it, and `NOT_TESTED_CEILING` in
  `scripts/tests/test_docs_site.py` fails the tests when it rises; every block marked
  "not tested" is a defect waiting to rot. Add one only with a real reason. It rose once, from 36 to
  41, when five commands of "After a restore" stood in running text, and
  moving them into blocks (which the rule above demands) made them count; the test's docstring
  says which. The private package index page rose it from 45 to 48: three command blocks of
  `private-package-index.md` need a package index that asks for a password or the id of a
  deployed project version, which the docs-examples job cannot reach without a new service; the integration tests of the
  "Private package indexes" paragraph below are the proof. The upgrade note of the next release rose it from 48 to 52 (two blocks with placeholders for names and ids, and the recovery `SELECT` and `UPDATE` of "Delegation needs the ADMIN role" in `upgrades.md`, which read and change a real database); its other commands run in `getting-started/run.sh`. "Why is my query or task waiting?" (M5c3b) rose it from 53 to 54 for `lakana query status QUERY_ID`, which needs the id of a query that is still queued; the tests of the wait line in `test_queries.py` assert the line the page shows. The upgrade note "A secret is bound to one use" rose it from 52 to 53 (placeholders for names; the commands run in `bind-a-secret/run.sh`). "Verify a download" in `deploy-with-compose.md` rose it from 53 to 54 (#130): its `pip download` and `gh attestation verify` need the network, the GitHub CLI and a published, attested release, which the example harness cannot provide. Sorting the pages (#55) lowered it from 55 to 48 by testing seven blocks (the new example `projects`, `uv lock` from `train-and-predict`, and the `*lakana-env` block, which now shows its anchor and is parsed) and giving the other 21 "moved as is" blocks a real reason; the reason `moved as is` is refused by `scripts/tests/test_docs_site.py`. The operator's guide (#57) rose it from 48 to 51 (the single backup procedure of `deploy-with-compose.md` replaced two blocks, so 50): the three blocks of its recipe "One instance per client" (two Compose stacks on one host, `docker compose ls`, the upgrade of one) need Docker and two complete stacks, which the example harness cannot provide. The health page (#45) rose it from 50 to 53 with three blocks that ask Docker about a running stack (`docker compose ps --all`, the disk use, the log filter that removes secrets); the health route, the queue listings and the version run in `getting-started/run.sh`. The restore (#200) lowered it to 52: the backup block is synced from `docs/examples/compose-restore.sh`, which `compose-smoke` runs. "Export and keep the audit log" and "Install without internet access" rose it from 54 to 58: the crontab line of the scheduled export, and three blocks that need the internet, Docker and an isolated machine (`pip download` with the offline `pip install`, the DuckDB extension download and copy, `docker save` with `docker load`); each was run by hand in a network-less namespace or is Docker's own, as the page says, and the example `audit-export` runs the rest. "What leaves the machine" then rose it to 59 with the `git grep` over a repository checkout that lists the network clients of our code. Lowering the ceiling when blocks become tested is still the expected direction.


### Output blocks tied to an example

A block that shows what a command printed names the region of `docs/examples/EXAMPLE/run.sh`
that prints it, instead of plain `<!-- output -->`:

````markdown
<!-- source: docs/examples/getting-started/run.sh#query -->
```bash
lakana sql "SELECT * FROM gold.station_ranking ORDER BY warmth_rank"
```

<!-- output: getting-started#query -->
```text
...what the command printed...
```
````

- **The region must print exactly the block.** Everything between `# docs:start NAME` and
  `# docs:end NAME`, standard output and standard error together, is the output. A block that
  shows only part of it, or output of a case the region does not run, stays plain
  `<!-- output -->` (and is counted as unchecked). Trailing spaces and blank lines at the end
  do not count; every other character does.
- **The runner captures.** With `LAKANA_DOCS_CAPTURE=DIR`, `scripts/run_docs_examples.sh` writes
  `DIR/EXAMPLE/NAME.txt` for each region it runs (a region that runs twice keeps its last run) and
  still shows the output in the log. Unset, nothing is captured; there is no default directory.
  The example's own `capture_start`/`capture_end` redirects nest inside and are unaffected.
- **Progress lines are not compared.** `lakana run` prints `ASSET  STATUS ...` (two spaces; the status one of `pending`, `ready`, `running`, `succeeded`, `failed`, `cancelled`, `upstream_failed`, `setting the table back`) whenever a poll sees a task change, so which lines appear and in what order depends on timing. `is_progress` in `scripts/docs_blocks.py` drops them from both sides; a page still shows the ones a reader sees, and `--fix` keeps the page's. The summary after them (three spaces, or a table) is compared.
- **The checker compares.** `scripts/docs_blocks.py --captured DIR` (or the same variable)
  compares each tied block with its file after both are masked (below); a mismatch names the
  page and line and prints a diff of the masked texts. Without a capture it checks only that the
  region exists in `run.sh` and says that the comparison was skipped. CI's `docs-examples (N/4)`
  shards run the examples with the variable set and upload their captures as `docs-captures-N`,
  even when a step failed; the `docs-examples` job merges them, compares, and uploads the merged
  captures as the artifact `docs-captures`, also when the comparison failed.
- **`--fix` refreshes.** `uv run python scripts/docs_blocks.py --fix --captured DIR` rewrites only
  the tied blocks that differ, and only the lines that differ after masking: a line that equals
  the run's after masking keeps the page's text (a page may show its own identifier). Run it
  on the unzipped `docs-captures` artifact of a red CI run, read the diff, and commit. It also
  copies *every* differing line, so a block that deliberately shows less than the region prints
  must be untied (plain marker) rather than fixed.
- **Masks.** The parts that differ per run are replaced on both sides before comparing, in this
  order. `MASKS` in `scripts/docs_blocks.py` is the one list; each row has a test in
  `scripts/tests/test_docs_output.py`, and a test fails if a rule is missing from this table.

| Name | Hides | Becomes |
|---|---|---|
| `uuid` | run, deploy and query identifiers | `<uuid>` |
| `timestamp` | ISO 8601 date and time, with or without seconds and zone | `<timestamp>` |
| `hash` | 40 to 64 hexadecimal digits: commits, SHA-256 digests | `<hash>` |
| `alert time` | the time of a health alert, `05 Nov 00:33 UTC` | `<alert time>` |
| `short hash` | the abbreviated commit id in `git commit`'s `[master 9445d5e]` | `[master <short hash>]` |
| `snapshot` | an Iceberg snapshot id, 16 to 19 digits | `<snapshot>` |
| `token` | `lakana_pat_`, `lakana_ci_` and `lakana_task_` tokens; a page shows `<secret>` for the secret part, and `--fix` writes it that way, never the real value | `lakana_pat_<key id>_<secret>` |
| `port` | the port after `localhost`, `127.0.0.1`, `0.0.0.0`, `[::1]` | `host:<port>` |
| `path` | absolute paths under `/tmp`, `/var/tmp`, `/var/folders`, `/home`, `/Users`, `/root`, `/run/user`, also after `file://` | `<path>` |
| `version` | `1.2.3`, with `.dev4`, `rc1`, `+g1a2b3c` | `<version>` |
| `duration` | a number with `ms`, `s`, `sec`, `seconds`, `min`, `minutes` | `<duration>` |
| `size` | a number with `KiB`, `MiB`, `GiB`, `KB`, `MB`, `GB` | `<size>` |
| `short id` | 8, 12 or 16 hex digits in a table cell (`│ `) or after the words `run`, `token` or `manifest`: a run's first eight characters, a token's key id, a manifest digest | `<short id>` |
| `package count` | the number in uv's `Resolved 44 packages`, which follows the public index | `<count>` |

After masking, in a line with a box-drawing character (`│ ┃ ┏ ┡ └` and so on) runs of spaces
and runs of `─ ━ ═` count as one, on both sides: a table pads every cell to its widest value, so
a masked value of another length (a 16 and a 19 digit snapshot id) would otherwise fail the
comparison. Inside table lines, padding and in-cell alignment are therefore not compared. Only the
comparison does this; `--fix` writes the run's real text, and when any line of a table changes it
takes the whole table (top border to bottom border) from the run, so the widths stay consistent.

Where a line wraps is not compared either: the terminal breaks a long line at its width, and
masked values of another length move the break. If the lines differ, the comparison joins line
`n + 1` to line `n` only when the terminal had to break there (`n`, a space and the first word
of `n + 1` do not fit in the console width), `n + 1` is not blank and has the same indent, does
not start a list item (`- `, `1. `), and neither is a table line (a line that starts with, or
holds two, box-drawing characters; `+---+` rules; `| a | b |` rows; `--- ---` headers). The
masks then run on the joined text, so a mask with a space in it (an alert time) matches across
a wrap. The width is the last `export COLUMNS=N` of the example's `run.sh` above the region (put the
export before the first region it should apply to; the checker's own environment never counts,
so the result does not depend on who runs it); with no export, the longest line that is not a table line, so there
one long unwrapped line in a block switches the joining off. Blank lines, indentation, spacing
inside a line, list items and tables still count, and one stray `│` in a sentence does not make
it a table. `--fix` keeps the page's own line breaks in a stretch where only the wrap differs.

A mask hides a change too, so add one only for a value that really differs per run, and
prefer a narrow pattern. Table width depends on the terminal: an example that quotes tables
sets `COLUMNS` as the page shows them (`export COLUMNS=120`).

### Text that leaves the repository

A file `lakana init` writes, the generated Compose file, a message the CLI or server prints and
the four PyPI readmes reach readers with no checkout, so they name a page by its URL
(`https://lakana-labs.github.io/lakana/how-to/projects/`), never by `docs/site/...`.
`scripts/tests/test_user_text_urls.py` fails on a `docs/` path in those files and on a URL
whose page is not under `docs/site`; add a new such file to its `LEAVING` list.

### Code blocks and their markers

Every fenced block under `docs/site/` is one of four kinds. A marker is an HTML comment on the
line directly above the fence (a blank line between them is an error); `scripts/docs_blocks.py` judges each block.

1. **Synced from a tested file.** The block must equal the file (or region) exactly, down to
   trailing whitespace. The path is under `docs/examples/`; a `#name` picks a region.

   ```markdown
   <!-- source: docs/examples/getting-started/run.sh#deploy -->
   ```bash
   lakana repo add analytics --kind local --url "$PWD"
   lakana deploy --env dev
   ```
   ```

   (The block's fence is shown with the outer fence of the example; on a page it is one fence.)
   A region is the lines between `# docs:start name` and `# docs:end name` in the file (in the
   file's own comment syntax: `#`, `--`, `//`, or `/* ... */`). Without `#name` the whole file is the block.
   `--fix` rewrites the block from the file.

2. **Syntax-checked, no marker.** A block in `python`, `toml`, `yaml` or `json` is parsed. It
   proves the block is well-formed and nothing more. A block in any other language needs one of
   the other three markers.

3. **Output.** What a command printed, in a `text` or `console` block or one with no
   language (any other language is an error). Not compared with anything; it must be pasted
   from a real run. The checker prints how many there are. Where an example can assert the
   stable lines of an output, it should (the tutorial's `run.sh` does for deploy and run).

   ```markdown
   <!-- output -->
   ```text
   lakana 0.8.1
   ```
   ```

4. **Not tested, with a reason.** The escape hatch, and it is counted.

   ```markdown
   <!-- not tested: installs from a checkout until the packages are published -->
   ```

Malformed markers (two in a row, a near miss such as `<!-- sorce: ... -->` above a fence, a missing reason,
a missing file or region) are errors, and the checker stops reading that page at the first one.
The code blocks in `docs/agent/` are not on the site and are not checked, which is why
the markers above are shown literally.

### Examples

An example is a directory `docs/examples/<name>/` with an executable `run.sh`, optionally a
`start.sh` and a `project/` tree of files the page shows (an example that only runs commands
needs no `project/`). `scripts/run_docs_examples.sh` runs each against its own
fresh local instance, with a private home, so it never touches your configuration or data.

To add one:

1. Create `docs/examples/<name>/run.sh`. It starts in an empty `$WORK` directory; `$EXAMPLE_DIR`
   is the example's own directory (read it, do not write to it). The file must be executable
   (`chmod +x`) and its first two lines must be exactly `#!/usr/bin/env bash` and
   `set -euo pipefail`; `scripts/tests/test_docs_examples.py` fails otherwise. A failing command
   is reported as `run.sh line N: <command>`, silent checks included.
2. Wrap each command a page shows in a region and add a check after it, so a command that
   silently does nothing fails:

   ```bash
   # docs:start deploy
   lakana deploy --env dev
   # docs:end deploy
   out=$(lakana --output json sql "SELECT count(*) AS n FROM bronze.readings")
   python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == [{"n": 4}]' "$out"
   ```

   The page shows the region; the lines between regions are the checks and stay out of it.
3. If the instance needs a non-default start command, put it in the `start` region of
   `start.sh`. The runner appends `--port <free port>` and runs the command in bash, after the
   private `HOME` is set, so it may set a variable for the instance as the page shows it
   (`LAKANA_RUN__INGESTION_ROOTS="{\"landing\": \"$HOME/landing\"}" lakana server start --local`).
   Keep it one simple command: bash then replaces itself with it, and the runner's stop reaches
   the server.
4. Files a page shows whole live under `project/` and are copied into the work directory by
   `run.sh`; the page syncs the block from the file path.
5. In the page, put a `source` marker above each block and run `uv run python
   scripts/docs_blocks.py --fix` to fill the blocks. Show real output below in an `output` block.

An example uses tiny literal data and never generates large inputs (`range(...)` with a large
bound, recursive queries, cross joins): every example runs in CI and on contributors' machines,
and a query's memory limit is best effort.

A change under `docs/examples/` is not a documentation-only change, so CI runs the example.

### Running everything locally

```bash
uv sync --locked --all-packages --group docs
uv run python scripts/docs_blocks.py            # check every block; add --fix to rewrite synced ones
uv run python scripts/generate_reference.py     # rewrite the generated references and the api-nav region (--check: fail when stale or left over)
uv run --group docs zensical build --strict     # build into site-build/ (a traceback after the message is normal on failure)
uv run python scripts/check_built_site.py site-build   # external resources, missing files and URLs outside /lakana/
uv run --group docs zensical serve              # preview on http://localhost:8000
uv run bash scripts/run_docs_examples.sh        # every example, or name one: ... getting-started
LAKANA_DOCS_SHARD=2/3 uv run bash scripts/run_docs_examples.sh   # only what CI's shard 2 runs
uv run pytest scripts/tests                     # the tests that read the site and the workflows
```

Each example keeps uv's cache and its worker's in its own private home; the runner shares no
cache between examples. A shared uv cache, kept between CI runs, was tried and removed: the job
took 12m34s with a cold cache and 12m48s with a warm one, because downloads are not what costs
time there.

The runner needs `git` and a free port, and takes at most `LAKANA_DOCS_EXAMPLE_BUDGET` seconds
(default 120) per example; an example that needs longer holds a number of seconds in a file `budget` next to its
`run.sh` (`train-and-predict` waits for a cron schedule and has 300), and the longer of the two applies. When an example fails, the runner prints the instance's log and then,
under the heading `logs of the runs that did not succeed`, the output of every run of that instance
that did not succeed (`lakana --output json runs list`, then `lakana runs logs RUN_ID`, for the
environment `dev`, which every example uses). That is where `uv`'s build output is: it is a task's
output, not part of the instance's log. It is best effort: at most 30 s, one line for a
command that fails, and the example's exit status is unchanged. In CI the `docs` job runs the
block check (without `--fix`), the generated-reference check, the
strict build and the built-site check, and uploads `site-build` as the `docs-site` artifact
(kept 7 days); each `docs-examples (N/4)` shard builds the `lakana` wheel of the commit (`uv build --package lakana
--wheel --out-dir DIR`) and runs the runner with `LAKANA_TEST_WHEELHOUSE=DIR`. The example
`train-and-predict` runs Python assets that train and load a scikit-learn model, and ends with a job on
`* * * * *` that must fire on the real clock: its `run.sh` polls `lakana runs list` for up to 150 s for a
run with `trigger=schedule`. Its project copies that one wheel into a folder
`wheelhouse` (a flat index for `lakana` alone, as the tutorial shows) and gets every other package
from the **public package index**, so the example needs a network connection, for `uv lock` and
for the worker's first build. Run it locally the same way: build the wheel into a directory under
`~/.cache` (not `/tmp`, which is held in RAM), set the variable, and run the runner inside the
memory scope. The runner's private `HOME` hides uv's cache and the Pythons uv downloaded; the
example sets `UV_PYTHON` to the interpreter that runs it, and the worker builds with its own cache.
`docs-examples` is a required check; `docs` is not.

**Shards (#363).** The examples run in four jobs side by side, `docs-examples (1/4)` to
`(4/4)`, because one after another they took 12.5 to 12.8 minutes against a 15-minute budget
(three shards at first; four since three ran up to 9.6 of their 10 minutes each).
`LAKANA_DOCS_SHARD=I/N`, with no example named, makes the runner ask
`python3 -m scripts.docs_example_shards select I/N` which examples are shard I: the longest first,
each to the shard with the least time so far, by the seconds in
`scripts/docs-example-durations.json` (an example without an entry counts 30 s). Every shard
computes the same split from the same directory, so each example runs exactly once; a shard and
example names together are refused. The required check `docs-examples` waits for the four,
fails for any shard that did not succeed (that shard's log names the example), and compares the
pages with the merged captures, which needs all of them. `scripts/tests/test_docs_example_shards.py`
fails for a new example without a recorded time, and for an entry whose example is gone. Record
the time the runner printed for it (each shard's log ends with a table `| example | N s |`), or
refresh the whole file from a run's logs when the shards drift apart:

```bash
gh run view RUN_ID --log > ~/.cache/lakana-docs-examples.log
uv run python -m scripts.docs_example_shards durations \
  --output scripts/docs-example-durations.json ~/.cache/lakana-docs-examples.log
```

`durations` writes the mean seconds per example over every row it finds.

The ingestion recipes of "Load data from a source" (`ingest-files`, `ingest-sql`, `ingest-rest`,
`ingest-generic`) are Python assets set up the same way: `lakana init --template` writes the
project (the asset under test is the one the CLI wrote; `run.sh` diffs it against
`project/assets`, and `test_ingest_templates.py` compares those files with `INGEST_TEMPLATES`), and
`ingest-files/use_wheelhouse.py`, which all four call, swaps the template's pin for the wheel. Their
sources are stand-ins that need neither Docker nor a server: the files and the SQLite database lie
below an ingestion root of the example's private `HOME`, named in the `start` region of
`start.sh`; the SQLite file is made with Python's `sqlite3` (`ingest-sql/orders_db.py`). It holds orders 1, 2 and 4 for
the first run and gets 3 and 5 before the second: 3 stands for a row committed late, which the
template's merge reads again behind its watermark (`LOOK_BACK`). The REST
API is `ingest-rest/stand_in.py`, an OData-style `http.server` behind TLS 1.2 or newer, with a CA
and a certificate for `localhost` and `127.0.0.1` that `make_certs.py` makes with `cryptography`
for each run; the bearer token is generated for each run too, so no secret is in the tree.
`run.sh` starts the stand-in in the background, waits for the port it writes, and stops it on exit.
`ingest-generic` reuses those two scripts. Under the local runner a task reaches `127.0.0.1`
directly.

The example `python-sample` runs `lakana init --template python`, which runs `uv lock` itself, so
the pin cannot be swapped between writing and locking. Instead `run.sh` puts the wheel into
`~/weather/wheelhouse` before `lakana init` and exports `UV_FIND_LINKS=wheelhouse`: uv takes lakana
from that folder and the lock names it as `registry = "wheelhouse"`, which the deploy accepts. The
wheel must be the CLI's own version (`run.sh` checks it): build it from the commit the workspace
was synced at. `test_project_init.py` fakes `uv` (`shutil.which` and `subprocess.run`), so its tests
need neither uv nor the network.

## CI runners

CI jobs run on the runner named by the repository variable `CI_RUNNER`; when the variable is not
set, which is the normal state, they run on GitHub's `ubuntu-latest`. The switch exists for a
maintainer who wants to run CI on a runner of their own.

- **What moves:** every job of `ci.yml`, `security.yml`, `conventional.yml`, `mutation.yml` and
  `security-deep.yml`, and the `release-please` job of `release.yml`, which only opens and merges
  the release pull request.
- **What never moves:** the macOS shards `unit (macos, N/4)` (macOS runs only on GitHub's own runners) and every job
  that builds or publishes a release. What users install is
  built on GitHub's runners, not on someone's machine.
- **A pull request from a fork always uses GitHub's runner.** The `runs-on` expression honours
  `CI_RUNNER` only for a push, a schedule, a manual run, or a pull request whose head branch is in
  this repository. On a public repository a fork's code would otherwise run on the runner's owner's
  machine.
- **Dependabot's pull requests always use GitHub's runner.** They execute dependency versions
  nobody has looked at yet, and a self-hosted runner runs a job with its owner's rights.
- `scripts/tests/test_workflows.py` pins all four rules; a new job must use the same
  `runs-on` expression.
- **A self-hosted runner needs Playwright's system libraries installed beforehand.** The jobs
  `ui (e2e smoke, N/2)` and `ui (e2e server)` run `playwright install --with-deps chromium`, which installs them with `sudo apt`; on
  GitHub's runners that works, on a runner of your own install them once (`sudo npx playwright
  install-deps chromium`) or give the runner's user that `sudo` right.

`scorecard.yml` is the exception to the switch: OpenSSF Scorecard publishes its results (for the
README badge) only from a GitHub-hosted job, so its one job is on `ubuntu-latest`.

A self-hosted runner runs whatever a pull request contains, as the user who started it,
including Docker. The fork guard keeps outside code off it, but a branch in this repository still runs on it, so
set the variable only while every collaborator is trusted, and delete it and remove the runner when
you no longer need them.

A self-hosted runner should run under a memory cap, and its job workspace and temporary
directory persist between jobs, unlike on GitHub's runners, so empty the temporary directory
before every job. The variable switches CI to the runner and back:

```bash
gh variable set CI_RUNNER --body self-hosted
gh variable delete CI_RUNNER
```

One runner runs one job at a time, so a pull request's jobs run one after another. The runner
must be running for a pull request's checks to finish; jobs wait in the queue while it is off.

## Memory limit

Every test run watches its own memory (`scripts/pytest_memory_limit.py`, loaded by
`packages/conftest.py` and `scripts/conftest.py`). A daemon thread measures the resident
memory of the pytest process and of every process it started (anonymous and shared pages;
file-backed pages are cache, not growth), four times a second. Over 6 GB it kills the
subprocesses, prints one line to the real stderr (what was measured, the limit, the variable,
the test that started last) and ends pytest with exit status **75**, which no test can catch.
A test that loops while it allocates therefore ends the run in a second or two instead of
taking the machine down: one did, at 23 GB, when a bound was removed to check that a test
would notice. It needs `/proc`, so it enforces on Linux only; elsewhere it says so once and
does nothing.

The default (6144 MB) is at or above the caps agents use for a test scope (`MemoryMax=4G`, or 6G for a full
suite): inside such a scope the cgroup kills the process first, and the kill comes without the
line that names the test. Under `pytest -n` only the controller watches and it counts every
worker, so the limit applies to the sum; a worker never starts its own. To get that
line, set `LAKANA_TEST_MEMORY_LIMIT_MB` below the cap (for a 4G scope, 3584); the cap also
counts page cache and `/tmp` files, which the watchdog's figure leaves out, so it can still
win. Run one test job at a time; the limit is sized for one.

Every run ends with a line such as `test memory: peak 2985 MB resident (pytest and child
processes), limit 6144 MB`. Measured peaks (2026-10-10): the whole suite run serially, 1.6 GB on
Python 3.13 and 3.11 (4.8 GB at `-n 4`) and 1.9 GB on 3.14 (4.2 GB at `-n 2`, the most that
fits comfortably; see "Running the unit suite in parallel"). Measured 2026-10-17 on
`packages/lakana-core/tests` alone, serial, no coverage, Linux x86_64: 797 MB on 3.14 against
592 MB on 3.13; the 3 GB against 1 GB of issue #72 does not show without coverage (with it, see
the `sysmon` bullet above). Measured 2026-10-22 on DuckDB 2.0.0, Python 3.14, `-n 3 --dist loadfile`,
no coverage, one package per run: `lakana-worker` 3.8 GB, `lakana-core` 2.3 GB, `lakana-server`
1.9 GB (DuckDB 2.0 starts I/O threads per query process; `async_threads` is capped at four per
processor thread). The integration files that start a server peak at 0.2 and 0.7 GB. If a
peak approaches the limit, raise the limit before the suite starts to fail. A warning line
follows if the watchdog's thread stopped.

It is a watchdog on resident memory, not `RLIMIT_DATA` (tried first and replaced): that counts
mapped memory, which DuckDB and the allocator never hand back, so it only grows over a suite
of 8,000 tests and, with coverage on, reached 8 GB while about 2.7 GB were resident. DuckDB
aborts there instead of raising.

- `LAKANA_TEST_MEMORY_LIMIT_MB=16384` raises it; `0` switches it off.
- It covers the test process, not a Lakana instance, a container or a script you start
  yourself. Run those under a limit of their own, for example
  `systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 <command>`.
- A test that counts open descriptors before and after (`/proc/self/fd`) uses
  `scripts.pytest_memory_limit.open_descriptors()`, not `len(os.listdir(...))`: the watchdog
  keeps a few `/proc` descriptors open for milliseconds four times a second, and the helper
  waits (on a lock the watchdog holds) until a sample is over.
- A burst between two measurements is not seen, so the systemd scope above stays the safety
  net for a run you cannot watch.
- A process whose parent died (a test's grandchild that outlives its parent) is adopted by
  `init`, is no longer linked to pytest and is neither counted nor killed: the session and
  process group are shared with the shell and other jobs, so no identifier tells it apart
  safely.
- A measurement reads `/proc/<n>/stat` of every process on the machine (about 7 ms with 550
  processes), so its cost grows with the machine's process count.
- A test must never need much memory to prove a limit: lower the limit instead (a 100 MB
  limit against a 200 MB child that lives for a second), and never run a query that
  generates data, or rely on the watchdog to stop a test.

## Query children in tests (M5c3a)

Every query runs in a child process of the worker (`lakana_worker.query_child`), so a test
that calls `execute_query`, `stream_query`, `describe_query` or `run_one` starts a real one. That
costs about 0.1 s per query (the probe measured 80 ms for `SELECT 42`); do not replace the
child by a mock to win it back.

- `packages/lakana-worker/tests/scripted_child.py` is a stand-in that misbehaves in one named
  way (`ok`, `no_end_marker`, `ignores_sigterm`, `stops_when_asked`, `exit_7_after_a_full_stream`,
  `full_stream_then_killed`, and more; read the file). A test points the executor at it with
  `monkeypatch.setattr(executor_module, "_ARGV", [sys.executable, str(SCRIPT), behaviour])`.
- A test of a stop uses the scripted child, or a statement that ends by itself within ten
  seconds (`_SLOW_SQL` in `test_executor.py` runs about 9.5 s), with a limit of a second or less,
  so a stop that fails still ends the test. The supervisor's own tests (`test_query_process.py`)
  start the clock after the scripted child's `READY` line on standard error, which it writes once
  its signal handling is set; an executor test that cannot see that line uses a limit of a second
  and a grace under it.
- The fixture `child_leak_check` (`packages/lakana-worker/tests/conftest.py`) points the
  children's private directories at a directory of the test's own and fails a test that leaves a
  directory or a process group behind. It is autouse for every test of the worker's package; tests in other
  packages that start children (the server's) have no such check. The same conftest resets `executor._DRAINING` around each test.
- Coverage does not follow into the child. Every line of `query_child.py` is therefore also run in
  the test process (`main`, `serve` and `run_cli`, with the process-level calls replaced), and a
  few tests start the real child to pin what only a process can show.
- A result is complete only when the supervisor's batch iterator ended normally. A test of a read,
  a write or a describe that ends the child after its last batch (`full_stream_then_killed`)
  asserts that nothing is returned, stored or committed.
- The query process runs in allow-list mode: it opens only `read_prefixes`, its temp directory and
  the CA bundle. A test that hands `execute_query` local files of its own passes their directories
  (`worker_test_helpers.local_prefixes(parameters)`, or `read_prefixes=(f"{tmp_path}/",)`); one
  that wants DuckDB's own spill passes `spill=True`. Every string of a list-valued variable must lie
  under a prefix, or the child refuses the request as `internal` before running anything.
- **HTTPS without a store: `packages/lakana-worker/tests/tls_fixture.py`.** `make_authority(dir,
  name)` makes a certificate authority with `cryptography` (a runtime dependency of `lakana-core`)
  and writes its certificate to a PEM file; `serving(directory, authority, certs, redirects)` serves
  a directory over HTTPS on `127.0.0.1` from a thread of the test process, with a leaf for
  `127.0.0.1`/`localhost` signed by that authority, answers listed paths with a `302`, and records
  every request line (`Serving.seen`). A test gives the query process the authority's file by
  replacing `lakana_worker.executor.query_ca_bundle` (the `authority` fixture of
  `test_query_allow_list.py` does). A second `make_authority` with another name is the foreign CA
  of a verification test. Why not a TLS SeaweedFS: this exercises exactly what DuckDB 2.0 broke
  (httpfs loading the CA bundle under the restriction, which does not depend on the server), runs in
  the unit tier under coverage with no container and no network, and plays what a store cannot: a
  second host, another authority, a redirect. The integration tier keeps plain-HTTP SeaweedFS for
  the presigned path, and `compose-smoke` the image.
- **The SDK's copy: `packages/lakana/tests/sdk_tls_fixture.py`.** The same authority and
  `serving`, plus `scripted` (an answer function per request, an endless body without
  `Content-Length`) for the `lakana.ingest` tests. Its name differs on purpose: test directories
  are not packages, so two modules named `tls_fixture` in one pytest session would shadow each
  other (the first imported wins).

**Integration tier.** `packages/lakana-server/tests/test_query_process_integration.py` runs real
children against Postgres and SeaweedFS, over a real HTTP server (`python_asset_helpers.serving`)
with the worker as a thread of the test process (`_worker`: `poll_once(only_queries=True)` in a
loop). It needs no wheel directory. About a dozen tests, two containers, one environment per test.

- `_long_sql()` is the one query that runs long by construction: a cross join of two
  `range(40000)` summed into one value, about nine seconds and almost no memory. A test that
  cancels it polls with a bound for `RUNNING` (10 s) and for the final status (15 s); a cancel that
  fails still ends the test when the query ends by itself.
- The killed write: `lakana_worker.pipeline.stream_query` is replaced by a wrapper around the real
  one that, once the first batch was handed to the catalog's writer, kills the child's process
  group (`os.killpg(pid, SIGKILL)` for each `REGISTRY.pids()`). The statement is
  `SELECT i AS id FROM range(500000) AS t(i)` (eight batches, so the child still waits on its pipe
  when the first is consumed). The test asserts the run is `FAILED` with the cause `internal`,
  no table, an unchanged object listing of the environment's storage and an empty registry.
  The cut at a batch boundary is pinned by the unit tests of `QueryChild._outcome` in
  `test_query_process.py` and `test_pipeline_writes.py` (the end-marker and exit-status
  parametrisations), not by this test: pyarrow itself rejects a cut in the middle of a batch.
- The race with the dequeue: `lakana_worker.poller.run_one` is wrapped so that, while the worker's
  session still holds the claimed row, the test sends the cancel from a thread and waits
  (`wait_for_lock_waiter`) until Postgres shows it blocked on that row.
- The cancel route under real grants uses real personal tokens, a group grant, an instance
  administrator and a task token from `bound_attempt`; no worker runs in that test, so every
  query stays queued and the test cancels what it leaves.
- Migration `0021` on Postgres is in `packages/lakana-core/tests/test_migrations_postgres_integration.py`.
- `test_the_sql_tasks_of_a_run_execute_in_query_children` (in `test_run_engine_integration.py`)
  counts the children of a three-asset run through `REGISTRY.add`.

Run them like every integration test, in a capped scope and with a base temp under `~/.cache`
(`/tmp` is a RAM disk):

```bash
systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0 timeout 600 \
  uv run pytest -m integration packages/lakana-server/tests/test_query_process_integration.py \
  --basetemp ~/.cache/lakana-pytest-m5c3a
rm -rf ~/.cache/lakana-pytest-m5c3a
```

## Lost workers on Postgres, with real processes (M5c3b)

Two integration modules in `packages/lakana-server/tests/` cover what SQLite cannot show.

- `test_lost_workers_integration.py` runs the worker as a thread of the test process and holds a
  write at the write fence (`_Hold`), or a deploy at its finishing call (`_DeployHold`), while the
  test ages the worker's heartbeat and runs the sweep. It shows both orders of sweep and commit,
  that the sweep never waits for a row lock (`FOR NO KEY UPDATE SKIP LOCKED`), that a deploy's
  `_hold` re-reads the row after waiting behind the sweep, that a SQL task's own write is left to
  the task's lease, that a task whose write committed before its worker died is ended `succeeded`
  once and not run again (a worker thread dies at `_keep_a_prior_stamp`; the key is all NULL, so
  no watermark could hide a repeat), that a reclaim which waits for a committing write sees its
  evidence (the lock statement in `_apply_task`), and the plan of the ended-queries read (`EXPLAIN` must name
  `ix_query_runs_environment_id_finished_at`).
- `test_lost_worker_processes_integration.py` starts `python -m lakana_worker` as a real child
  (`_worker_process`): a new session, an environment built from scratch (a private `HOME`, the
  database URL, the master key, a lease of 3 s, a renew interval of 1 s, a scheduler interval of
  0.5 s), a wait until its row is in `workers`, and `SIGKILL` of its process group in a `finally`.
  It kills workers while they run a long query and asserts the cause `worker_lost`, one completion
  event, a cancel answered at once, and a surviving worker that takes the scheduler role and ends
  the dead one's query. It also runs the real `SchedulerLoop` against Postgres to show the pause
  of a new leader and of a leader that stalled.
  The module also holds the tests of worker kinds (M5c3c): `_worker_process(..., takes=)` sets
  `LAKANA_WORKER__TAKES` in the child. One shows that a worker with `pipeline` leaves an ad-hoc
  query queued with `no_worker_of_kind` (its heartbeat has advanced meanwhile) until a worker with
  `interactive` starts and runs it; another starts three interactive workers and shows that one
  user's third long query waits with `user_limit` while a worker is free (`auto` gives 2) and
  starts when one of theirs is cancelled.
- `test_running_cap_integration.py` (M5c3c) runs no worker: it calls `poller._next_query` and
  `pipeline._start` itself on real connections, because SQLite has one writer and cannot show a
  race. It holds a claimer between the advisory lock and its commit, waits with
  `wait_for_lock_waiter` (also requiring an ungranted `pg_locks` row with `locktype = 'advisory'`)
  until the second claimer queues behind it, and shows that the second then counts the first's run
  and is refused, that a claim for another principal does not wait, and that a claim which waits
  longer than the lock timeout is refused and leaves its run `QUEUED`.
- Every bound is computed from the settings the test uses: lease + renew + 5 s (the new leader's
  pause) + one scheduler interval + a margin. No sleep decides a pass; a wait polls a condition with
  a bound. A test that starts workers must kill and reap them itself, whatever happened.
- Migration `0022` on Postgres is in `test_migrations_postgres_integration.py`: the table, both
  columns, both indexes, the downgrade, and that a `RUNNING` run without a worker id is ended once
  it started a lease ago.

**Checks and the rollback (M5d-a).** The same three tools, and one more module. Rules 1 to 3 of
the section above hold unchanged: bounds from the settings, no sleep decides a pass, a test that
starts a process reaps it.

- `test_checks_races_integration.py` runs no worker loop. Every test builds a task through the
  product (`_task`: deploy from git, a run started over REST, the real worker builds the table
  through the query pipeline so that the stamp is the product's) and lets the worker "die" at its
  first count (`_Died`, a `BaseException` raised from `checks.count_check`). It then calls
  `roll_back`, `record_result`, `mark_written` and the scheduler's tick on real sessions, because
  SQLite has one writer and cannot show an interleaving. A hold forces the order: `StatementPause`
  (`integration_helpers.py`) is a `before_cursor_execute` listener that pauses one named thread
  right before a statement that contains a given text is sent (one permanent listener per engine;
  pauses register with it, because removing a listener while another thread iterates the engine's
  listeners raised `deque mutated during iteration`), or a wrapper around a module attribute that
  tests already wrap (`service.write_metadata`, `commit_metadata_pointer`,
  `ProtectedWriteGuard.record_pending`, `tasks.record_attempt`). A test then waits until the other
  side is *seen waiting* in `pg_locks` (`wait_for_lock_waiter`), never for a time, and releases
  the hold. `write_check(repository, table, check, sql, *, on_failure=None)` writes and commits a
  check file. Reuse these, do not add a hook to product code. What it kills: a rollback that holds
  a row lock across its upload (it blocks a lease renewal), a rollback that discards another
  writer's commit or is undone by a late write, a database error that records a rollback twice or
  not at all (a commit that landed without its acknowledgement, one that never arrived, a backend
  cut with `pg_terminate_backend`), a result recorded for a claim the scheduler is taking back, a
  stamp that lands after a reclaim's snapshot, and a stale reclaim that ends a written task
  `succeeded`, `failed` or retried.
- `test_lost_workers_integration.py` gained five tests with the worker as a thread. Where the
  other module drives the catalog, these drive a whole task: the real write is paused between its
  `FOR SHARE` on the task row and the stamp's `UPDATE`, and a lease renewal or a reclaim is *seen
  waiting* behind it; a worker killed at `record_attempt` after its rollback leaves a task that
  the next claim ends `failed` from the recorded results, with one write run and one count; a worker
  killed inside a count has that count ended `FAILED` by the sweep with `CHECK_LOST_MESSAGE` and
  the next claim checks without building again (the asset appends NULL keys, so a second build
  would show 6 rows, not 3). The module's `_retire_leftovers` cancels every active run of the
  database before each of the five, so a test added after them must not rely on a run left by an
  earlier one.
- `test_lost_worker_processes_integration.py` gained one test with a real process:
  `test_a_worker_killed_between_write_and_checks_is_checked_by_the_next_claim` kills the process
  group once the task is stamped and a count is `RUNNING`, and at **every poll** of both phases the
  task is not `succeeded` and its downstream tasks have not been ready. It ends with one write run,
  the killed count `FAILED`, and the second worker's counts `SUCCEEDED`. The deploy runs in the
  test process, because the worker processes have no repository root.
- The Python-asset tests of the first module (a second attempt rolled back to before the first, a
  foreign commit leaving the table alone, an input that is not published) need no wheel directory
  and no `uv`: `acquire_venv` returns a lease on nothing and a scripted `Runner` stands in for the
  harness process, while the deploy, the code tarball, the bound token, the check phase and every
  write over HTTP are real. A test of a real `uv` environment is the example `train-and-predict`.
- Migration `0024` on Postgres is in `test_migrations_postgres_integration.py`
  (`test_0024_on_postgres`): the twelve added columns with type, nullability and default, the table
  `check_results` with its constraint, indexes and foreign keys, old rows reading as "no checks",
  the downgrade and the upgrade again. **A migration's own test pins its revision,** because the
  next migration moves `head`: it upgrades to its own revision, never to `head`, and asserts a
  revision only after a move to exactly that revision.
  `test_migration_tests_pin_their_revision.py` (unit tier, reads the source) fails otherwise. The
  older tests `test_0014_...` to `test_0022_..._on_postgres` still upgrade to `head`; they look at
  columns and rows, not at the revision, and only the second half of the rule holds for them.
- Migration `0025` on Postgres is in the same file (`test_0025_on_postgres`): the three nullable
  columns of `tables` with type and length, no foreign key, an old row reading as "no note", the
  downgrade and the upgrade again. `keep` end to end is in `test_run_engine_integration.py`:
  `test_a_kept_table_stops_later_runs_until_its_checks_pass`,
  `test_a_task_of_another_project_is_refused_a_kept_table` and
  `test_the_project_default_reaches_the_run`.
- Migration `0030` on Postgres is in the same file (`test_0030_on_postgres`): `tables.kept_unchecked` as
  a boolean, not null, default false, a seeded note reading `False`, the downgrade (the note stays)
  and the upgrade again. The SQLite test in `test_migrations.py` seeds the same row.
- Migration `0031` is tested the same way (`test_0031_on_postgres`, `test_0031_adds_the_stack_of_earlier_marks_of_a_kept_table`):
  `tables.kept_earlier` as JSON, not null, an existing note reading `[]`, a new row reading `[]`
  by the server default, and `tables.kept_snapshot_id` as a nullable bigint reading null. The stack's rules are in `test_runs_kept.py` (push, clear, restore, the
  limit, the sequence of another run's clear before a set-back, the mark of unknown checks) and the
  set-back's in `test_catalog_rollback.py` (restore, the chain of two, a set-back that changes
  nothing or does not commit, no entry gives unknown checks).
- Migration `0032` is tested the same way (`test_0032_on_postgres`,
  `test_0032_adds_the_unknown_mark_of_a_kept_table`): `tables.kept_unknown` as a boolean, not null,
  false for an existing mark, the downgrade and the upgrade again.

## The coverage ratchet

What is measured is `[tool.coverage.run] source` in `pyproject.toml`: the four packages'
`src` trees **and `scripts/`**. The CI tools are gates themselves (documentation-only,
licence, conventional-title, mutation and coverage checks), so they are held to the same
100 % of lines and branches; their tests in `scripts/tests` are omitted from the
measurement. Adding a file under `scripts/` needs no configuration, only its tests. The
pattern excluded for this, in `[tool.coverage.report] exclude_also` beside
`if TYPE_CHECKING:` and `raise NotImplementedError`, is `if __name__ == "__main__":`, which
runs only when Python executes the file as a program; each script's `main()` is tested
directly. Shell scripts are not measured.

`scripts/coverage_ratchet.py` reads `coverage.json` (produced by
`pytest --cov --cov-report=json`) and fails on any of three checks (or, with a message
naming the file and the next step, on a missing or malformed report or baseline):

- **Staleness** — the report must be newer than every `.py` file under the coverage source
  roots configured in `pyproject.toml`. Without this the ratchet scores whatever the last
  `--cov` run happened to leave behind, which is not a hypothetical: it once reported
  `100.00%` for a tree containing a module it had never measured, because the intervening
  `pytest` run carried no `--cov` flag. **A percentage from a stale report is not a
  measurement.** Files the coverage `omit` setting leaves out (`scripts/tests`) do not count,
  but editing any other file under `scripts/` makes the report stale, so run the full
  `pytest --cov --cov-report=json` before the ratchet.
- **The floor** — `DEFAULT_FLOOR = 90.0`, overridable with `--floor`.
- **The baseline** — the highest coverage ever recorded, in `.coverage-baseline` (currently
  `100.00`). Coverage may rise but must never fall below its own baseline, even if that value
  is above the floor.

Because of the staleness check, `pytest` and the ratchet are one step, not two: run
`pytest` **with** `--cov --cov-report=json` or the ratchet will refuse the report rather
than quietly score the previous one.

CI measures coverage on Python 3.14 only (the 3.11 run executes the same tests without `--cov`,
which saves about a third of its time). The three 3.14 shards each keep their coverage data
(`.coverage.shard-N`, artifact `coverage-data-N`); `test (3.14)` combines them with `coverage
combine` into one report and prints `coverage report --skip-covered --show-missing`, so a branch
that fails the ratchet is named by file and line in that job's log. One shard alone is not the
suite: the ratchet only ever reads the combined report.

The ratchet is a CI gate (see "Local gates"); to reproduce a failure locally, run it the
same way CI does, over the whole suite: one package at a time, the data combined (the loop in
"Running the unit suite in parallel"; one run over everything outgrows the memory limit, #488).

**To raise the baseline** after adding tests that increase coverage, pass `--update`:

```bash
uv run python scripts/coverage_ratchet.py --update
```

This overwrites `.coverage-baseline` with the new (higher) percentage and should be
committed alongside the tests that earned it. The script only ever writes a value when
`current` is used to update — it does not lower the file automatically, so don't hand-edit
`.coverage-baseline` downward to "make room."

### Where the ratchet stands today

`ci.yml`'s `Coverage ratchet` step runs the script with no `--floor` override, so it uses
`DEFAULT_FLOOR = 90.0` — the same invocation as "Run it locally" above. The M0 `--floor 0`
concession was removed early (commit `3f52492`), which also set a real baseline from
measured coverage instead of `0.00`.

Because `.coverage-baseline` currently reads `100.00`, the baseline is the stricter check
right now: any PR that drops total coverage below 100% fails even though it would still
clear the 90% floor. Both checks run on every CI invocation; whichever is higher wins. As
the codebase grows and coverage settles below 100%, the 90% floor becomes the operative
bound instead — but it has been enforced, unconditionally, since the baseline was set.

## The duplication ratchet

Nothing else in CI sees copied code, so the `lint` job runs **jscpd** (MIT) over the Python source
of the four packages, `packages/*/src`. Tests are not scanned: they repeat their setup on purpose.
jscpd is a pinned npm tool in `tools/jscpd` (`package.json` with one exact version,
`package-lock.json`, Dependabot's `npm` entry, and the licence check of
`scripts/check_licenses.py` reads the lock like any other). CI runs `npm ci --ignore-scripts` there
and never a bare `npx jscpd`. A clone is a block of at least 70 tokens (`--min-tokens 70`, about
six lines) that appears twice. The licence check needs the `license` field of every package in the lock,
which npm writes from version 11 on: regenerate the lock with `npx npm@11 install
--package-lock-only` in `tools/jscpd` when a Dependabot update leaves it out (the check then
fails and names the packages).

`scripts/duplication_ratchet.py` reads jscpd's JSON report (`jscpd-report/jscpd-report.json`,
ignored by git) and the baseline `scripts/duplication-baseline.json`: duplicated lines, number of
clones, and both per pair of files. It fails when either total **rises**, and then prints every
block of the pairs that gained duplication as `file:start-end ↔ file:start-end`. Names are
relative to each package's `src`. When the numbers **fall** it passes and prints the new numbers;
lower the baseline in the same pull request with `--update`, or the freed room will be spent by
the next change.

Run it locally the same way CI does:

```bash
npm ci --ignore-scripts --prefix tools/jscpd
tools/jscpd/node_modules/.bin/jscpd packages/*/src \
  --format python --min-tokens 70 --reporters json --output jscpd-report --silent
uv run python scripts/duplication_ratchet.py          # check
uv run python scripts/duplication_ratchet.py --update # lower the baseline after a fall
```

`--update` never records a rise. To regenerate `tools/jscpd/package-lock.json` use npm 11 or
later: older npm versions leave out the `license` fields that the licence check needs.
How to handle a finding: "Duplicated code" in `docs/agent/conventions.md`.

The interface gets a second run in `lint`, over `ui/src` with its tests left out, and its own
baseline, `scripts/duplication-baseline-ui.json` (names relative to `ui/src`):

```bash
tools/jscpd/node_modules/.bin/jscpd ui/src --format typescript,tsx --ignore "**/*.test.ts,**/*.test.tsx" \
  --min-tokens 70 --reporters json --output jscpd-report-ui --silent
uv run python scripts/duplication_ratchet.py --report jscpd-report-ui/jscpd-report.json \
  --baseline scripts/duplication-baseline-ui.json          # add --update after a fall
```

## The `ui/` gates

The web interface (`ui/`, see "The `ui/` project" in `docs/agent/conventions.md`) has its own
gates. Locally, `npm --prefix ui run check` runs the first eight in this order and stops at the
first failure; CI's `ui (gates)` job runs them one step each, `ui (gate proofs)` runs
`prove-gates` beside it, and the two Playwright suites run in `ui (e2e smoke, N/2)` and
`ui (e2e server)` (the required check `ui` waits for the four).

| Gate | Command | Where it runs |
|---|---|---|
| ESLint (strict type-checked, react-hooks, jsx-a11y strict, TSDoc, the server-only-through-`src/api/` rule, no `process`/`Buffer` in `src/`, no HTML sink: Vitest's `ui/scripts/lint-ban.test.ts` proves the ban fires once per fixture), no warning allowed | `npm --prefix ui run lint` | `check`, CI `ui (gates)` |
| Prettier | `npm --prefix ui run format:check` (`format` rewrites) | `check`, CI `ui (gates)` |
| tsc (strict, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`) | `npm --prefix ui run typecheck` | `check`, CI `ui (gates)` |
| dependency-cruiser (no cycles, no orphans, `src/api/` a leaf, no dev dependency or test in shipped code) | `npm --prefix ui run depcruise` | `check`, CI `ui (gates)` |
| knip (unused files, exports, dependencies) | `npm --prefix ui run knip` | `check`, CI `ui (gates)` |
| Vitest with coverage (thresholds 90 %) | `npm --prefix ui run test:coverage` | `check`, CI `ui (gates)` |
| build | `npm --prefix ui run build` | `check`, CI `ui (gates)` |
| size-limit (`ui/.size-limit.mjs`: the gzipped first load (the entry chunk and the chunks `index.html` preloads with it), each chunk loaded on demand on its own (a shared limit, or a named one found by the start of its file name for a larger screen such as Home), CSS, fonts; a raised limit needs a sentence in its pull request) | `npm --prefix ui run size` | `check`, CI `ui (gates)` |
| UI coverage ratchet | `python3 -m scripts.ui_coverage_ratchet` | CI `ui (gates)` |
| the real build served by lakana-server (`test_ui_real_build.py`: `/` carries `csp.txt` byte for byte, every `src`/`href` of the page and `url()` of its CSS answers 200 with its type, the licence notices are plain text; skipped without a build) | `uv run pytest packages/lakana-server/tests/test_ui_real_build.py` | after `build`: locally, CI `ui (gates)` |
| Playwright on Chromium (`e2e/shell.spec.ts` in light and dark, roomy and compact: axe with WCAG 2.2 AA tags on every area, the menu, the profile picture dialog and the palette, no request to the API but the session routes it stubs with `page.route` (`e2e/session.ts`), no console error (the user menu's picture loaded from a `blob:` URL under the policy), deep link styled, theme without a flash, a theme switch that fades the colours over 200 ms (none on first paint, none under reduced motion), no horizontal scroll at 768 and 1280 px; the root's `color-scheme` (so the scrollbars) dark under a dark system and under an explicit dark choice, light under a light system and under an explicit light choice; the palette centred over the whole window in its upper third at 1280 and 600 px, axe clean; at 320 px (WCAG 1.4.10 reflow) no horizontal scroll, axe, the folded menu by keyboard; at 600 px wide and 500, 900 and 1400 px high the top bar keeps its height and the content starts right below it; `e2e/glyphs.spec.ts`: the seven glyphs at 12 px saved as screenshots in colour, greyscale and the three colour-blindness simulations (the contrast numbers are checked by Vitest in `src/status/colour-vision.test.ts`); `e2e/mark.spec.ts`: the mark at 16 and 32 px saved as screenshots, sail, hull and float apart at 16 px, and `public/favicon.png` equal to `public/favicon.svg` at 32 px (`node ui/scripts/favicon-png.mjs` renders it again); `e2e/sign-in.spec.ts`: an invite link, the required passkey step with the CDP virtual authenticator, sign out everywhere and a passkey sign-in against stubbed routes, axe on each step; `e2e/cli-sign-in.spec.ts`: `lakana login`'s two confirmation pages against stubbed routes in light and dark, roomy and compact: axe on both, both by keyboard alone with the focus never on `<body>`, the browser sent to `http://127.0.0.1:<port>/` with the code and the state (`page.waitForRequest`), the confirmation at 320 px with a 64-character host inside the window and no sideways scroll, no request but the stubbed ones and no console error; `e2e/notices.spec.ts`: every runtime dependency's licence notice in the build; `e2e/connect.spec.ts`: Connect a repository's four steps against stubbed probe and repository routes, axe after each step in light and dark, roomy and compact, no page error, no API request but the stubbed ones, and at 320 px no horizontal scroll; `e2e/catalog.spec.ts`: the Catalog list against stubbed routes (`e2e/session.ts` answers the list by its query: a second page behind a cursor, a search with column and description matches, none, an empty namespace, a 404 for an unknown environment): axe on the list, the open Sort by, a search, no hits and an empty namespace in light and dark, roomy and compact; at least 24 px measured between the visible content of every two neighbouring cells; the tree, the search field, Sort by, the headers and Show more by keyboard alone (the focus stays in the tree, goes to the first new row after Show more); Back and Forward restoring the place, the search and the sort; at 1280 × 800 every column of the list visible without the list scrolling; compact rows lower than roomy ones; at 320 px no horizontal scroll and the list scrolling in its own focusable region; a hover on a time, a size and a health word reaching the element that carries its title; a plain click on those cells opening the table and a drag that selects text in a cell not opening it; the stored place opened from Home and Show all tables showing all tables; `e2e/table.spec.ts`: a table's page (`/catalog/prod/gold.sales/fact_sales`, stubbed by `e2e/session.ts`, with a preview that is queued at the first poll and has finished at the second, a definition whose file holds markup and a right-to-left override, a health with a failed check and, for an administrator, readers, a lineage with an asset, a job and a connection, and the grants, row filters and column masks of the table) in light and dark, roomy and compact: axe on all seven tabs (Overview, Columns, Preview with the Value panel open, Lineage, References, Health and Access), the seven tabs wrapping at 320 px in both themes with every tab inside the window and passing axe, with the editor of a table's and of a column's texts open and Escape returning focus to Edit, and in Defined in's file dialog with Escape returning focus to the path; Overview fitting 1920 × 1080 without scrolling; the blocks of Overview, Lineage, References, Health and Access as flat panels (the surface tint, a 1 px hairline, no shadow, a heading at least 4 px above the body text, more space between panels than inside one, no room kept for an empty copy hint) and none on Columns; at 320 px every status word of Health and Access on one line and every list that does not scroll inside its panel; compact rows of Columns, Health and Access lower than roomy ones; and in Chromium the tabs, Edit, Save, the file dialog and the grid by keyboard alone (the grid is one Tab stop with the arrow keys inside), no horizontal scroll at 320 px on any tab with Columns, Preview, Health's checks and the Lineage drawing scrolling in their own focusable region, Lineage, Health and Access by keyboard alone (a step button that goes when nothing is further hands the focus to the drawing's region, and a drawing's table is reached through the list), the full-screen lineage opening, passing axe and returning the focus on Escape and on Close, the full-screen lineage filling the window at 1920 × 1080 and 1280 × 800 with Close in view, lineage boxes growing to their text up to 320 px and ending a longer line in "…" on the tab and in full screen, Preview and the Lineage drawing using the whole window width while the other tabs keep 1040 px, the preview's dense data grid (32 and 26 px rows, numbers right-aligned in tabular figures, a sticky header, a row-number gutter left out of a selection, axe in dark compact), no word in Health's tables broken inside at 1280 and 1920 px and no "null" in Observed, Health's signals scrolling in their own region at 320 px, the editor's empty counter taking no room, Back and Forward between the tabs and the lineage depths, a click on a table in the drawing opening its page with no page load, Readers › opening Health with the readers heading focused, Health details › and Lineage ›, the file view showing markup as text and marking the override, a preview that waits, shows its rows and its masked column and is cancelled when the tab is left, a hover that changes nothing and a click that shows a cell whole in the Value panel, a Catalog row and a Home link opening the table page, no request but the stubbed ones on every tab, `/grants`, `/row-filters` and `/column-masks` included, and no console error; `e2e/manage.spec.ts`: the table's Actions menu and the catalog's Dropped, Expiring and Orphans views against stubbed routes (`e2e/manage.ts`) in light and dark, roomy and compact: axe on the open menu, the Drop, Maintain now and Retention dialogs, each view, an Undrop and the References tab, and on the open menu and the Rebuild dialog of a table a project defines; the menu, each dialog and each view by keyboard alone with the focus returning to Actions or landing on the result line; a git-owned table's menu described by the sentence that names its file (`aria-describedby` on the menu, Drop `aria-disabled`); no horizontal scroll at 320 px in each view with the lists in their own focusable region; no request but the stubbed ones and no console error; Back and Forward between the views keeping the place; and in Chromium a Drop that asks for the passkey touch, sends again and lands on Dropped; `e2e/places.spec.ts`: the Catalog tree's place menu against stubbed routes (`e2e/places.ts`, with `dev` protected) in light and dark, roomy and compact: axe on the open menu, the New namespace, New external table and Delete namespace dialogs, a refused registration, the form of a protected environment and the notes after a create and a delete (one with the result line beside it: two status regions at once); the menu and each dialog by keyboard alone with the focus returning to Actions or landing on the note; a namespace with tables offering Delete disabled with the sentence that says why (`aria-describedby` on the menu); no horizontal scroll at 320 px with the menu and each dialog open; no request but the stubbed ones and no console error; and in Chromium a Delete namespace that asks for the passkey touch, sends again and lands on the environment; `e2e/artifacts.spec.ts`: an artifact's page against stubbed routes (`e2e/artifacts.ts`, with its own tree holding `prod.ml` and the list's `ml.model` row of kind `ARTIFACT`: a current version of two files, a deleted one, the bytes of each file and the reader's open upload) in light and dark, roomy and compact: axe on the Overview, the Versions tab with a version's files open, the Delete version dialog and its summary, the uploads block after a Withdraw, the Drop dialog and the note after a drop; the tabs, Files, Download, Delete…, Withdraw and Drop by keyboard alone (Escape pressed only once the focus is inside the Delete version dialog) with the focus returning to the button that opened a dialog or landing on the version's row, the status or the note, never on `<body>`; a git-owned artifact's menu described by the sentence that names its file, Drop `aria-disabled` and Delete… on its versions still offered; no horizontal scroll at 320 px on each tab, the versions and the files below them scrolling in the versions' focusable region; Download saving the file under its last path segment with its bytes (Playwright's download event), and a file whose bytes differ from the manifest saving nothing and saying so; no request but the stubbed ones and no console error; and in Chromium a Delete… that asks for the passkey touch, sends again and shows what was freed; `e2e/editor-csp.spec.ts`: the SQL editor styled in its open shadow root by constructed stylesheets, with no `<style>` element, no violation of the content security policy and no console error, and lint's error underline and gutter marker drawn without a `data:` image, in every engine; `e2e/queries.spec.ts`: the Queries screen against stubbed routes (`e2e/queries.ts`: `prod` protected and `dev`, a run queued, running and succeeded, a run that fails at line 2, column 1, a run that goes on until cancelled, a page, the CSV and the spreadsheet, Saved with an own, a shared and an administrator's view of a private query, Recent, and the saved queries' create, update and delete) in light and dark, roomy and compact: axe idle, running, cancelled, with a result, with an error, with the Save and Delete dialogs, on a saved query without its SQL, and folded at 320 px with the Queries ▾ popover open; the picker, the editor, Run with Ctrl/⌘ Enter, the grid, both downloads (the files' bytes), Cancel with Escape and the library by keyboard alone, the focus never on `<body>`; Escape closing the completion list first and cancelling only on the next press, no cancel sent before it; a protected environment's mark and word visible in greyscale (screenshots through `e2e/colour-vision.ts`, which `glyphs.spec.ts` shares); no horizontal scroll at 320 px with the library folded, with a result, an error and the Save dialog; the editor's chunk not in the first load and loaded on opening Queries, and Format's chunk not before Format; a failed run's underline over the word at its line and column and its gutter marker drawn under the policy (`e2e/csp.ts` records violations), Go to moving the cursor there and an edit taking the mark away; and no request but the stubbed ones, no console error and no violation through a run, a download, a failed run, Save as new, Delete and Recent; `e2e/settings.spec.ts`: Settings' left list against stubbed routes, with the places by keyboard alone, the Section picker at 320 px, the old `/access/` addresses redirecting with Back skipping them, and a person without rights given a percent-encoded address to a hidden place (`/settings/%73ign-in/invites`) told so, with no request for it; `e2e/access.spec.ts`: Settings › Access › Groups, a group's page, Grants and What I may do against stubbed routes (`e2e/access.ts`, which keeps the groups, members and grants a test changes) in light and dark, roomy and compact: Grants' filters, list region and Remove and Add grant by keyboard alone with the passkey touch sent before the write and the focus back on Add grant after Escape, a group's Add, Remove and Delete group (the name typed) by keyboard with the passkey touch sent before each write and the focus never on `<body>`, no write control in a managed instance and no request carrying `override_managed`, at 320 px no horizontal page scroll with each list in its own focusable region and a 300-character name on one line, What I may do for a person without admin rights, axe on every page and dialog, no request but the stubbed ones and no console error; `e2e/status-repositories.spec.ts`: Settings › Status and Repositories against stubbed routes (`stubRepositories` and `stubReader` in `e2e/repositories.ts`) in light and dark, roomy and compact: Status with the detail (bars, the workers lost first, the scheduler) and without it (no Workers heading and no scheduler read), the repositories by keyboard (a row opened, Test access with the git host's refusal, Replace key with the passkey touch sent before the POST and the focus on Deploy keys after Done, Delete repository), a reader who sends no write on the list, a page and `/new`, and at 320 px no sideways page scroll with the list scrolling in its region and Edit inside the window, axe clean, no request but the stubbed ones and no console error; `e2e/environments.spec.ts`: Settings › Environments against stubbed routes (`stubEnvironments` and `stubMara` in `e2e/environments.ts`) in light and dark, roomy and compact: an environment created, its retention set, protected, unprotected and deleted with its files, all by keyboard, with the passkey ceremony's requests before each guarded write, the focus never on `<body>` and axe clean with each dialog open; mara, who manages dev and dev-old only, seeing usage and the deleted environment's actions for those alone, no request for prod's usage and no write but dev's retention; an option's sentinel value in Create environment found only in the one POST body, never in an address, a console message, a storage or the page; and at 320 px no sideways page scroll with the list and the leftover directories scrolling in their regions and the dialogs inside the window; `e2e/tokens-secrets.spec.ts`: one's own API tokens, Settings › Access › Tokens and Secrets against stubbed routes (`stubTokensSecrets` and `stubSecretsOfDevOnly` in `e2e/access.ts`) in light and dark, roomy and compact: Create a token by keyboard on `/account` with the passkey touch sent before the POST, the token shown once with the `lakana login` line, the focus on API tokens after Done and the token gone from the page, its inputs and both storages; Tokens' Owner filter, Show more and a personal and a task token's Revoke by keyboard with the focus on the section's heading and never on `<body>`; Secrets for a person who manages `dev` only (a linked `prod` named as refused and never read, the picker offering `dev` alone) with Add secret by keyboard, the touch before the POST and the value in no request address, console message, storage entry or the page afterwards; at 320 px no horizontal page scroll on Tokens and Secrets with each list scrolling in its own region and every dialog inside the window; axe on each page and dialog, no request but the stubbed ones and no console error; `e2e/policies.spec.ts`: Settings › Access › Row filters & masks and Access file against stubbed routes (`stubPolicies` in `e2e/access.ts`) in light and dark, roomy and compact: the filters, both list regions, Show more, a Delete and Add filter or mask by keyboard with the passkey touch sent before each write and the focus on the section's heading or back on Add filter or mask, a managed environment's policies marked with no Delete and a managed instance with no write and no request carrying `override_managed`, a policy manager who is no administrator landing on Row filters & masks with no read of `/access/status` or `/access/export`, Download access file by keyboard saving `access.yaml` with the stubbed text and the text's sentinel nowhere on the page, in either storage or in a console message, at 320 px no horizontal page scroll with each list in its own focusable region, a 4,000-character expression wrapped in its cell and every dialog inside the window; axe on each page and dialog, no request but the stubbed ones and no console error; `e2e/sign-in-configuration.spec.ts`: Settings › Sign-in, Invites, Recovery links and Configuration against stubbed routes (`stubSignInAndConfiguration` in `e2e/access.ts`) in light and dark, roomy and compact: the session lengths saved, an invite created and a recovery link issued by keyboard with the passkey ceremony's requests before each write, the link shown once and gone afterwards and the focus back on the button that opened the dialog; the old `/access/invites` and `/access/recovery-links` landing on Sign-in with their search and hash and Back skipping them; Configuration's find field, region and Clear search by keyboard with a master key's value a faulty stub sent in no page text, storage or console message; at 320 px no horizontal page scroll with each list in its own region, names on one line and a long value wrapped in its cell; hidden characters in a value and an invite marked; axe on each page and dialog, no request but the stubbed ones and no console error; `e2e/jobs.spec.ts`: the Jobs list and the job, run and task pages against stubbed routes (`e2e/jobs.ts`: the environments `dev` and `prod` (protected), the job `orders_hourly` in `dev` with a paused schedule, a running run in `dev`, a queued run of no job and a failed run of no job with the run's own message, beside the job `daily_sales` of `analytics` in `prod` with a schedule and a Slack notification through a secret, a failed run of seven tasks with one `timeout`, one `upstream_failed` and a failed check, a running run that reads cancelled once its cancel was asked for, a task's log in three pieces, and Run now, Pause, Resume schedule, Cancel, Resume and Show rows) in light and dark, roomy and compact: on the list, axe on Running now, Failed, needs attention and All jobs, the open filter, the Cancel and Pause dialogs opened from rows and a search with no hits; at least 24 px between the visible content of every two neighbouring cells at 1920 px; the filter, the search, Cancel (the focus back on it after Escape), Pause (the focus on Resume after it) and a job's link by keyboard alone; Run's press not opening its row and the queued run showing under Running now; at 320 px no horizontal scroll with the three lists scrolling in their own focusable region; the filter remembered across a reload and Back and Forward keeping the filter and the search; no read while the tab is hidden and one at once when it shows; no request but the stubbed ones and no console error; on the pages, axe on the job page, a failed run, a task with its log, the Show source dialog, the Cancel and Pause dialogs and a run the server does not know; the breadcrumbs from a task back to its run, its job and Jobs; Run now, Pause schedule and Resume schedule, Cancel, Resume, Show source, Log and Show rows by keyboard alone, the focus staying on Run now, returning to the button that opened a dialog, landing on the run's heading after Resume and never on `<body>`; no horizontal scroll at 320 px with the tasks and the attempts scrolling in their own focusable region and the log wrapping; a log that grows over three polls (offsets 0 and the end of each piece) and is read no more once complete; no read of a running run while the tab is hidden (`visibilityState` and the `visibilitychange` event) and one read at once when it is shown again; no request but the stubbed ones and no console error; and Home's failed run opening the run's page; on the Runs and Deploys tabs (the stubs add every run, the ended ones with a run of a selection and a resume on a second page, and the deploys of `analytics` in `prod`: a dry run, the current deploy, a failed one, an older deploy by an account since deleted and an older rollback, with the current version, each version's jobs and a rollback running at its first read and succeeded at the second), axe on the Runs tab after Show more, the Deploys tab and the rollback dialog of a protected environment; the tabs, State, Job, Show more, Read again, Roll back to this… and the dialog by keyboard alone, the focus back on the row's button after Escape and never on `<body>` once the rollback ended; the dialog naming the job both versions define and the one the rollback brings back, the rollback followed from running to succeeded with the list read again and the older version current; Back and Forward keeping each tab's filters and a job's link to its repository's deploys; a resume's link to the run it continues and no control that starts a run; at 320 px no horizontal scroll with each list in its own focusable region and the tabs inside the window; no read of the runs or of the rollback while the tab is hidden; no request but the stubbed ones and no console error; `e2e/queries-clickthrough.spec.ts`, `e2e/jobs-clickthrough.spec.ts` and `e2e/connections-clickthrough.spec.ts`: the click-through checks the review of M7 parts 4–6 made by hand, once in light and roomy (Queries: the toolbar above the editor and the library beside it, completion of a namespace's tables and an alias's columns with the hover over a column against a stubbed catalog, the Value panel, a result larger than a spreadsheet at 320 px, a name pasted from two lines through the real clipboard (Chromium), the library's filter across a reload, the dialogs' sentences, a saved query read offline, the focus after Save, Delete and a cancel, and the chunks of the first Format; Jobs: a job page's filters and run in the history, the dialogs' sentences, the focus after a cancel on a run's page and under Running now, a start that waits, the links underlined in the accent, the chunks of each tab and the rollback dialog; Connections: the focus as keys are added to the limit and removed); each also walks its pages with Tab and requires a focus ring at every stop (`e2e/focus-ring.ts`). A check the app does otherwise than the review expected is skipped with the issue it waits for; `e2e/hidden-characters.spec.ts`: every stubbed answer with a right-to-left override in each text (see "The hidden-character check" below) on Home, the Catalog list, tree, search and views, all seven tabs of a table's page with the Value panel, the full-screen lineage, Defined in's file and both text editors, every dialog of the table's and the place menu's Actions, a queued maintenance, an Undrop, a created namespace and a refused registration, an artifact's tabs and dialogs, Settings' Access and Sign-in places, one's account and a sign-in notice, an invite link's landing, `lakana login`'s two confirmations, the four steps of Connect a repository, the Jobs list's three sections, its open filter, the Cancel and Pause dialogs of a row and a search, and a job's page, the Pause and Cancel dialogs, a failed run with the Show source dialog, a task's page with its attempts and its log (each piece of it marked) and a run the server does not know (its 404 sentence), the Runs tab with a selection's patterns, the Deploys tab with its Repository menu, the rollback dialog and the note after a rollback, Groups, a group's page with its Remove and Delete group dialogs, Grants with its Remove dialog and Add grant's environments, What I may do and a person's grants and groups, Tokens with the Revoke dialogs of a personal and a task token, one's account with its API tokens, Secrets with its Delete and Set use dialogs, Row filters & masks with both Delete dialogs and Add filter or mask's environments, and Access file with its changes by hand and Download access file's environments, and Configuration's values: each step showing a named stubbed value with its mark and no override shown as itself anywhere, no request but the stubbed ones; the check's positive control (a raw override in a text node, an `aria-label`, an `alt`, an `<option>`'s `label`, a submit button's text, an SVG title and the document title found, a form field's value left alone); what `poison` changes per answer type; and what `apiTypes()` reads from `src/api`) | `npm --prefix ui run e2e:smoke` | locally after `check`, CI `ui (e2e smoke, N/2)` |
| Playwright against a real lakana-server (`ui/e2e-server/home.spec.ts`, `sign-in.spec.ts`, `catalog.spec.ts`, `table.spec.ts`, `manage.spec.ts`, `places.spec.ts`, `artifacts.spec.ts`, `queries.spec.ts`, `jobs.spec.ts`, `user-picture.spec.ts`, `settings.spec.ts`, `access.spec.ts` (Groups, a group, Grants and What I may do: a group made, filled, granted a role from Add grant, its grant seen on the group's and the person's pages, removed and the group deleted; a person's own grants; axe in light and dark) and `status-repositories.spec.ts` (Status for an administrator with its Workers and Scheduler and the version `GET /api/v1/info` answers, and for a reader of one environment without Workers; an HTTPS repository registered through the API, opened from the list, its default ref edited after the virtual authenticator's touch and deleted; a reader sees the list and the page without Connect, Edit, Delete or Test; axe on the list and a page in light and dark), `tokens-secrets.spec.ts` (an invited person's own token made on `/account` after the passkey touch, working as a Bearer token on `GET /api/v1/whoami` and refused (401) once revoked; an administrator finding a person's token through the person page's link and revoking it; a person with `MANAGE_SECRET` in one new environment landing on Secrets with that environment alone, adding, rotating (Rotated filled) and deleting a secret, with no read of the other environment's secrets; axe on Tokens and Secrets in light and dark) and `environments.spec.ts` (an administrator creating an environment under the server's data directory after the passkey touch, setting its retention, protecting and unprotecting it, deleting it and finding it among the deleted, restoring it and deleting it with its files; a person granted `VIEWER` and `MANAGE_ENVIRONMENT` on one environment with no Create, Set retention, usage and Delete but no Protect; a fresh environment's Leftover directories saying Nothing left over; an old leftover directory deleted after the passkey touch went stale, the file gone and the touch asked before the delete (#735); axe on the list and a page in light and dark), `policies.spec.ts` (an administrator adding a row filter and a column mask after the passkey touch, seeing both on the table's Access tab and deleting them after `ageThePasskeyTouch`; a person with `MANAGE_POLICY` alone landing on Row filters & masks, creating and deleting a filter, and told they cannot use Access file; an environment exported through Download access file, planned and applied, its policies then marked ⌂ Git with no Delete and the scope In sync; axe on both places in light and dark) and `sign-in-configuration.spec.ts` (an administrator's Configuration showing the bootstrap secret, the master key and the database URL as secret, set, with neither their text in the answer nor on the page; new session lengths saved after `ageThePasskeyTouch` with the touch sent before the PATCH, and put back; an invited person told they cannot use Configuration, with no read of it; axe on Sign-in and Configuration in light and dark), each test setting the state it needs through the REST API, so none depends on another file having run first; config `ui/playwright.server.config.ts`: the server starts on port 4174 with a fresh SQLite database under `ui/.e2e-server/`, serves the production build and is stopped at the end; links come from the bootstrap token through the REST API; setup link and passkey sign-in after sign out with the CDP virtual authenticator, the link's own secret (`s=`) gone from the address bar and the history state, the local link's automatic sign-in (the server runs with `LAKANA_UI__LOCAL_SIGN_IN`) with no Back or Forward step showing a `#`, a used link, the session cookie (HttpOnly, SameSite=Strict, per port), the cookie alone refused without the page's CSRF token while a new tab of the origin stays signed in, the token kept in `localStorage` and removed at sign-out, the 127.0.0.1 redirect, a cross-origin POST refused while the same POST from the origin reaches the route, an invited person with no access, axe in light and dark; a profile picture refused with the server's 422 sentence, set, shown in the user menu from a `blob:` URL with no console error (so no content security policy violation), and removed; Home: a newly invited administrator's first sign-in with the welcome and the whole tour by keyboard to Finish, recorded and not shown again after a reload, Skip on the welcome recorded, Get started with "Add a passkey" done after the passkey step, Get started hidden across a reload and brought back from Help, no update note on a development build even after a version was seen (the note shown and hidden once: Vitest), the status box saying Down with the no-worker sentence (the e2e server runs no worker), Connect a repository answering the refused host-key check (503 with Retry-After) with the server's sentence and the next step, and axe on the welcome, a tour step, Home and Help in light and dark at 1280 and 320 px; the Catalog: each test creates its own environment and looks only at it, an administrator drilling down from All tables through an environment and gold to gold.sales and back, a search finding a table by a column name and widening from an empty namespace to all, a header and Created sorting in the address and after a reload, a reader granted one table seeing only it with its owner and counting only it, a person with no access seeing no Catalog and the permission state on `/catalog`, and axe on the real list, a search and an empty namespace in light and dark at 1280 and 320 px; a table's page (`table.spec.ts`, each test in its own environment): an administrator opening a table from the list and seeing its seven tabs (no Defined in for a table no project defines), editing the description to Edited here and going back to the text from Git, a reader granted the table seeing its owner and last read but no Edit, a reader without the right to run queries seeing the Preview line with no preview sent, leaving the Preview tab cancelling the queued preview (the query reads CANCELLED; no worker runs), an administrator seeing a reader by name under Who has access, a reader's Access tab saying Read yes, Write and Drop no with the next step and asking for no grants, row filters or masks, a reader's Health tab sending `readers=false` and showing only how many people read the table, an administrator's Health tab asking for the readers (no `readers=false`) and showing the names section while a reader's shows only the count (the `ACTIVITY_VIEWED` entry itself is covered by the server's `test_view_audit.py`; the e2e server runs no worker, so no one has read a table, and the suite stays on the REST API), the lineage of a table no project defines saying nothing is there, and axe on all seven tabs in light and dark at 1280 and 320 px; `manage.spec.ts`, each test in its own environment: an invited administrator with a passkey dropping a table, finding it in Dropped and restoring it (the table answers 404, then 200), Retention on tomorrow's day putting the table in Expiring with its rule, Maintain now leaving a QUEUED query, the References tab of a table without a model saying none are declared, a local administrator without a passkey refused with the sentence the page shows for the server's 409 `NoPasskeyError`, word for word, and the table kept, and Orphans listing a table whose only project version is past, with Drop; that version is one of the three writes the suite makes straight into the server's SQLite file, as a deploy needs a git host and a worker; `places.spec.ts`, each test in its own environment: an invited administrator with a passkey creating a namespace from the tree, seeing it there and deleting it (the namespace then answers 404), the same delete after the person's passkey touch went stale (`ageThePasskeyTouch` moves it an hour back in the SQLite file, the suite's second direct write: the server answers `StepUpRequired`, the page asks for a new touch and sends the delete once more), an administrator registering an external table over a CSV the test writes under the environment's `raw/` and landing on its page with the focus on its heading and `id` and `amount` under Columns, and a person who may delete a namespace but not read the table in it refused with the server's sentence, the namespace kept and Cancel returning the focus to Actions; `artifacts.spec.ts`, each test in its own environment with an artifact stored through the REST API as the SDK stores it (`uploadArtifact`: begin with the manifest, put each file, commit): an administrator opening it from the Catalog list (the row's second line says Artifact) and downloading a file whose saved bytes equal the uploaded ones, an invited administrator with a passkey deleting the current version (the row then reads Deleted and current moves to the version before) and dropping the artifact, landing on its namespace with the note (the artifact then answers 404), a person withdrawing an upload they began from the page (`beginUploadAs`, with the page's session and CSRF token; their uploads then list none), and a local administrator without a passkey refused with the sentence the page shows for the server's 409 `NoPasskeyError`, word for word, and the version kept; `queries.spec.ts`, each test in its own environment, with a worker the file starts in `beforeAll` and stops in `afterAll` (`startWorker`): `SELECT 42 AS answer` run from the editor, seen in the grid and downloaded as CSV (`"answer"\r\n42\r\n`, the server quotes every name of the header row) and as a spreadsheet (a file starting with `PK`), `SELECT 1\nFROM` failing with the underline, the gutter marker and Go to line 2 under the content security policy with no console error, a saved query shared with everyone seen and opened by an invited person who may query and refused (403) to one who may not, Recent showing a run again after a reload with its result, and an administrator seeing an invited person's private saved query without its SQL (Run off) and deleting it, and a long query left for Home, found running in Recent and cancelled there; `connection-tests.spec.ts`, with a worker of its own like `queries.spec.ts`: Test all in an environment of two connections that reach nothing, counted "0 OK, 2 not OK" once the worker has tested them; `jobs.spec.ts`, each test in its own environment with a repository deployed by `recordDeployedJob` (the suite's third direct write: a project version whose manifest declares one table and the job `daily_sales` with a schedule and `notify:`, its succeeded deploy, the environment's pointer to it and the job's schedule row): an administrator seeing the job's facts (Notifies · Slack · via secret `alerts-webhook`, the schedule in words with its next run), Run now queuing a run that shows as Queued (the server runs no worker) with the focus kept on Run now, Cancel asking first and showing Cancelling…, and the run reading Cancelled once a worker the test starts (`startWorker`) and stops carries the cancel out; Pause schedule and Resume schedule changing the Schedule fact, across a reload; a reader of the environment (`READ_ENVIRONMENT`) seeing the same facts with no Run now, Pause, Resume or Cancel and no secret's name; and axe on the job page, its queued run and the run's page in light and dark at 1280 and 320 px with no horizontal scroll; on the Jobs list, with the job deployed in two environments whose repositories share a tag the search finds: an administrator seeing it under All jobs in both, filtering to one with the choice kept across a reload, Run queuing a run that shows as Queued under Running now, and Cancel moving it out once a worker the test starts carries the cancel out; a reader of one environment seeing only its job, no control and no other environment in the filter; and axe on the list in light and dark at 1280 and 320 px with no horizontal scroll; on the Runs and Deploys tabs, with a repository deployed twice by `recordDeployHistory` (which `recordDeployedJob` calls with one version: a project version and a succeeded deploy for each, the last current): an administrator seeing the current version and Roll back to this… on the older one only, the dialog listing the job both versions define and the one only the older defines as back, and Roll back queuing a rollback that stays queued with the note saying it is rolling back and the focus back on the button; a reader of the environment seeing the deploys with no button; the Runs tab listing a run queued through Run now; and axe on both tabs and the dialog in light and dark at 1280 and 320 px with no horizontal scroll; `settings.spec.ts`: an administrator's places and the old addresses, a person without rights seeing Status only, and axe) | `npm --prefix ui run build`, then `npm --prefix ui run e2e:server -- --project=chromium` | CI `ui (e2e server)` (Chromium); Firefox and WebKit nightly in `canary.yml` job `ui-browsers` |
| every gate fails on a violation | `npm --prefix ui run prove-gates` | after `build`: CI `ui (gate proofs)` |
| copied code (jscpd + ratchet) | see "The duplication ratchet" | CI `lint` |
| npm audit and osv-scanner over `ui/` and `tools/jscpd` locks, level `low`, but for the time-limited allowlist (see "Process" in `docs/agent/security.md`) | `python3 scripts/npm_audit.py ui tools/jscpd` | `security.yml` `npm audit` |
| licences of the npm locks | `uv run python scripts/check_licenses.py` | `security.yml` `static` |

- **The UI coverage ratchet.** `scripts/ui_coverage_ratchet.py` combines lines and branches of
  Vitest's `ui/coverage/coverage-summary.json` into one number, with a floor of 90 %, against
  `ui/.coverage-baseline` (100.00 from the start). It refuses a report older than `ui/src`.
  After a passing run that raised coverage, record it with `python3 -m scripts.ui_coverage_ratchet
  --update`; never lower the baseline or add a coverage exclusion to fit.
- **`prove-gates`** (`ui/scripts/prove-gates.mjs`) copies `ui/` and the build into a scratch
  directory (`$GATE_PROBE_DIR`, default `~/.cache/lakana-ui-gate-probe`; CI uses
  `$RUNNER_TEMP/gate-probe`), links in the server sources Vitest reads (`SERVER_SOURCES`:
  lakana-core's `health_signals.py` and `health_record.py`, whose effects and reasons the interface
  must have words for; a test reading another file outside `ui/` adds it there), writes one
  violation per gate (an unsafe `any`, a conditional hook, an
  image without alt text, an undocumented export, a `fetch`, `window.fetch`, `navigator.sendBeacon`
  or `axios` outside `src/api/`, `process` and `Buffer` in `src/`, bad formatting, three type
  errors, an import out of `src/api/` and a cycle, an unused file, an untested branch, a size limit
  of 1 B, an 80-token copy), runs the gate and requires it to fail with the expected message; one
  probe checks the other way round, that a `fetch` inside `src/api/` is accepted. The nine ESLint
  probes share one ESLint run (one run takes 15 s locally, about 23 s in CI; nine runs made the
  proofs take 290 s of what was then one `ui` job, #586): all their files are written at once and each probe must
  find its rules in the lines ESLint prints about its own files. ESLint runs with `--max-warnings
  0`, so each of those lines alone fails the run; the allowed file passes when ESLint says nothing
  about it. The verdicts live in `ui/scripts/gate-verdict.mjs`, tested by Vitest
  (`gate-verdict.test.ts`). When a step before a gate fails (the coverage probe's `test:coverage`,
  say), the probe's message ends with that step's exit status and the end of its output. It prints
  one line per probe and fails naming every gate that got its probe wrong: fix the gate, not the
  probe. Each gate's run may take 360 s (`TIMEOUT_MS`), since the coverage probe runs the whole
  Vitest suite (182 s in CI on 2026-11-17); a run cut by the timeout is judged a leak. It refuses a `$GATE_PROBE_DIR` that is not empty and lacks its marker file
  `.lakana-ui-gate-probe`, so it never empties a directory it did not make. It needs a build (`npm
  --prefix ui run build`) and `npm ci --ignore-scripts --prefix tools/jscpd`. The axe gate's proof
  is the smoke test's positive control (an image without alt text must be found); the audits prove
  themselves in `security.yml` on a lock written at run time with `lodash@4.17.20`.
- **The hidden-character check** (#669). A server value shown without `VisibleText`,
  `markedText`, `t()` or `problemText` (see "The `ui/` project" in `docs/agent/conventions.md`)
  shows a bidi control or a zero-width character as itself, so a name can hide or reorder what
  a person reads. `ui/e2e/hidden-characters.spec.ts` walks the screens against the stubs of
  `e2e/session.ts`, `manage.ts`, `places.ts`, `artifacts.ts`, `connect.ts` and `jobs.ts`, handed
  a page from `poisoned(page)` (`ui/e2e/hidden.ts`): every route a stub adds answers with U+202E
  at the end of each string, and matches the address and reads the body with the override taken
  out, so the stubs need no change. The addresses it opens carry the override too
  (`/catalog/prod%E2%80%AE/…`), as the page's own links then do.
  - **What stays as it is, per answer type.** Each object of an answer is read as the
    `src/api` type it fits (`ui/e2e/api-types.ts` parses `src/api` with the TypeScript parser;
    the type with every key of the object and the fewest other fields). Its codes keep no
    override: the fields whose type is a union of string literals (through aliases, lists and
    `Other["field"]`), and the `string` fields `STRING_CODES` in `hidden.ts` names per type, each
    because the interface translates it (a check's `outcome`, a health summary's `item`, a
    task failure's `code`, which an unknown value shows as `internal`). A code the interface
    compares (a lineage node's `kind`, a probe's `state`, a search hit's `kind`) is
    typed in `src/api` as a union of the server's values, so the check finds it by type (#682).
    The same name in another type carries the override (`PermissionOut.reason` does,
    `GraphNodeOut.kind` does not). Also left alone everywhere: ids (`id`, `*_id`) and any string
    equal to one of the answer's ids (a lineage edge's ends), upper-case constants (`USER`,
    `READ_TABLE`), an error's `type` (`StepUpRequired`), times and the empty string. A test pins
    what `apiTypes()` finds; a change to `src/api` that changes it fails there, so read the type
    before accepting the new list. A new `string` code joins `STRING_CODES` only with what the
    interface does with it.
  - **Which type each object is read as.** `typeOf` falls back to a partial fit and breaks ties
    by declaration order, so a new `src/api` type or a changed stub could move an object to a
    type whose codes include one of its free-text fields, which would then carry no override.
    `READ_AS` in `hidden.ts` pins the pick for every object the stubs answer, by its sorted keys
    as JSON (`readKey`); the spec's own objects for `poison` have their table in the spec. After
    each test the spec fails on an object read as another type or not listed (the message gives
    the line to add, once its fields are checked); a test fails when `typeOf` no longer picks what
    an entry says; and the last test, once every walk has passed in its worker, fails on an entry
    no stub serves any more. A code inside a free-form field (a health signal's `observed` and
    `band`) is typed too, in an interface that extends `Record<string, unknown>`, so the object
    is read as a type. A union of object types is written as one interface per member
    (`TriggerOut` in `src/api/jobs.ts`), since the parser reads interfaces only. The lineage stubs
    are written `satisfies GraphOut`, so a kind outside the unions fails the typecheck.
  - **What else a walk fails on.** A walk (`walk(...)` in the spec) fails on an error the page
    throws and on a file of the build (`/assets/`) that did not load, so a missing chunk shows as
    the cause instead of a value not found.
  - **What each step asserts.** `expectMarked(page, label, values)` names one or more stubbed
    values the step's screen shows and requires each with its mark (`fact_salesU+202E`) inside
    the open dialog, or else `<main>`; the shell's own marks (the user menu's name) do not count.
    A step proves its screen read its own answer only when a named value comes from that answer:
    a table's or place's name also reaches the page from the address (which carries the
    override) and an artifact's from its header, so a step that names one of those proves only
    what the next step needs (the loaded entry's Actions button). A tab or a dialog that reads
    its own answer names a value from it where the answer holds one the screen shows
    (Retention its rule's `ana`; the artifact's Lineage stub holds only the artifact, which the
    tab does not name, so that step rests on the header and the raw scan); then a screen that
    got no poisoned answer, or went down a branch a poisoned code sent it (a search hit shown as
    a name hit when its `kind` was poisoned), fails. Then, softly so one
    run names every screen, it requires no U+202E in a text node (an SVG `<title>` included), the
    document title, the text of an `<input type="button">`, `submit` or `reset`, or an
    `aria-label`, `aria-description`, `aria-roledescription`, `aria-valuetext`,
    `aria-placeholder`, `aria-braillelabel`, `aria-brailleroledescription`, `title`, `alt`,
    `placeholder` or `label` attribute. An open menu of the interface's own words gets the raw
    scan alone (`expectNoRaw`). A `<textarea>`'s text and an input's value are left out, as a
    person edits the text as it is, and the scan does not enter shadow roots (the interface
    attaches none). Each test ends by requiring that every API request went to a stubbed path
    (the stubs' `STUBBED` lists, as the other smoke specs do): an unstubbed one would reach
    `vite preview` and get no poisoned answer.
  - A failure names the element, such as `text in ul.connect-keys > li > code: SHA256:…⟨RLO⟩`.
    It runs in `npm --prefix ui run e2e:smoke`, so in CI's `ui (e2e smoke, N/2)` and the required
    check `ui`; `npm run check` runs no browser. **A new screen, dialog or state that shows
    server text adds its walk to the spec, naming a value it shows.** How far it reaches depends
    on the branches the fixtures take (the search fixture has a translated hit so the language
    is walked). An ESLint rule was the other option of #669; it was left out because it cannot
    tell a server value from the interface's own text without knowing where a value came from,
    so it would flag every `{name}` that already went through `t()` or miss one passed through
    a local variable.
- **Playwright.** Pull requests that touch `ui/` run the smoke test on Chromium; the full
  three-browser suite (`npm --prefix ui run e2e:all`) runs on release. The tests run against
  `vite preview` of the production build, which sends the content security policy of
  `ui/csp.ts`, so a CSP violation shows as a console error. Every axe check runs through `runAxe`
  (`ui/e2e/axe.ts`: the WCAG 2.2 AA tags, without React Aria's live announcer). In Firefox it first
  makes ARIA element reflection (`ariaLabelledByElements` and the other `aria*Elements`) answer
  null, so axe reads the attributes: Playwright's Firefox 159 crashes the page ("Page crashed") when
  axe 4.14 reads `ariaLabelledByElements` of an element React rendered (#700). Remove it once a
  Firefox update reads the property without crashing. `e2e:server` runs the sign-in,
  Home, Catalog, table page, Queries and profile picture tests against a real `lakana-server` instead (Chromium per pull
  request, Firefox and WebKit nightly); its tests share one server and one database and run one
  at a time, every person and environment they create has a unique name, and each test sets the state it needs
  through the REST API (the helpers in `ui/e2e-server/helpers.ts`), never relying on a test of
  another file. The e2e server runs no worker, so a test of a worker job asserts the page's
  no-worker answer. The exceptions are `queries.spec.ts`, whose queries must run, and
  `connection-tests.spec.ts`, whose connection tests must end: each starts a
  `lakana-worker` on the server's database in `beforeAll` (`startWorker` in the helpers, with the
  server's settings from `playwright.server.config.ts`) and stops it in `afterAll`; stopped with
  SIGTERM, the worker drains and removes its heartbeat, so the server counts no worker again. Locally, `npx --prefix ui playwright install firefox
  webkit` works without `--with-deps`, but WebKit does not start until the system libraries
  (`sudo npx playwright install-deps`) are installed; CI covers it.
- **When it runs.** The three `ui (…)` jobs run on drafts too, and each skips its steps when a
  pull request provably changes nothing under `ui/`, `tools/jscpd/`, the three ratchet scripts,
  the UI duplication baseline, lakana-server's `ui.py` and `test_ui_real_build.py`,
  `packages/lakana-server` or `packages/lakana-core` (for `e2e:server`), or `ci.yml`; the three
  ask that with the same step (`scripts/tests/test_workflows.py` holds them equal). Each has a
  time budget of 10 minutes (`"ui (*)"` in `scripts/time-budgets.toml`), the waiting job `ui`
  one of 5. A suite that outgrows its job gets a job of its own the same way (a new
  `ui (…)` job that `ui` waits for), not a larger budget.
- **Vitest workers.** `ui/vitest.config.ts` caps Vitest at 8 workers (its default is one per CPU
  but one): 19 workers on a 20-thread machine needed more than the 4 GB cap for the coverage run.
  CI's 4-CPU runners keep the default of three.
- **Async waits.** `ui/src/test/setup.ts` sets Testing Library's `asyncUtilTimeout` to 3 s (the
  default is 1 s): a file's first render can take longer under the coverage job's load (#657). It
  stays below Vitest's 5 s test timeout, so a missing element fails with Testing Library's own
  message and the DOM rather than "Test timed out"; raise both together. A wait that is met
  returns at once; a test that expects something *not* to appear gives its own short `timeout`.
- **Locally.** Use Node 26: `~/.local/share/node/current/bin` first on `PATH`, or any Node 26
  (`node --version` must print `v26.*`). Run npm under the memory cap with `TMPDIR` off `/tmp`:

  ```bash
  export PATH="$HOME/.local/share/node/current/bin:$PATH"
  mkdir -p ~/.cache/lakana-ui-tmp
  systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0 \
    env PATH="$PATH" TMPDIR=$HOME/.cache/lakana-ui-tmp npm --prefix ui run check
  npx --prefix ui playwright install chromium   # once; no --with-deps locally
  npm --prefix ui run e2e:smoke
  ```

### Reviewing a part of the interface

The maintainer reviews each part of the interface in a locally running instance and records it
in the pull request, in this form:

```markdown
### Review of part N — YYYY-MM-DD   (the first one is "part 1a")
- Browser and version:
- Operating system:
- Window size and zoom:
- Theme (light, dark, system):
- Density (roomy, compact):
- Findings (area — what — must fix / should fix / later):
```

## Time budgets

CI's time is the main cost of each change, so it has budgets, enforced like coverage. They live in
`scripts/time-budgets.toml` and `scripts/time_budgets.py` checks them; its messages name the job or
the test, the budget, and the next step.

- **Minutes per job.** `[jobs]` maps check names (`fnmatch` patterns, the first match wins) to
  minutes: a `unit` shard at most 15, each integration job at most 15, the collecting jobs and
  the small ones less. The job `time budgets` runs last (`!cancelled()`, so also after a failure),
  reads the jobs of its own run attempt from the API (`actions: read`, nothing else) and fails for
  a job that took longer than its budget, or that has none. `scripts/tests/test_workflows.py`
  finds a job without a budget before CI does. It is not a required check: a red `time budgets`
  says the suite outgrew its shards. Next step: find what grew (the job's slowest steps,
  `pytest --durations=20`), make it faster, refresh `scripts/test-durations.json` if the shards
  drifted apart, or add a shard; a budget is raised only with the maintainer's agreement.
- **The slow-test ratchet.** A unit test that takes `threshold_seconds` (2 s) or more — setup,
  call and teardown, as the 3.14 shards in CI measure them, under coverage — must carry
  `@pytest.mark.slow`, and the number of tests that carry it may fall, never rise: the baseline is
  `scripts/slow-tests-baseline.json`. The 3.14 shards write junit reports
  (`--junitxml=junit-N.xml -o junit_family=xunit1`: only that family names each test's file), in
  which `scripts/pytest_selection.py` adds the property `slow=true` to every marked test;
  `test (3.14)` runs `time_budgets.py slow-tests --confirm junit/*.xml` over them. One
  measurement in a busy shard is noisy (between two runs a test of 0.6 s once took 2.4 s), so an
  unmarked test over the threshold is run again, serially and under coverage. The first run is
  cheap: one pytest process per package (one per package in that run's flagged tests, usually
  four to six), where most flagged tests fall below the line as noise. Each test still at or over
  `confirm_seconds` then runs again in a fresh pytest process of its own, since one after
  another in a shared process the later ones were timed under the load the earlier ones left
  and unrelated tests of 2.5 to 2.7 s failed. That is at most `SOLO_CAP` (10) tests, the ones
  nearest the line, each up to twice: at most 20 more processes of about 7 to 9 s each (some
  4 s of start-up, the test itself at 2.5 s or more, and its warm-up), about 3 minutes. A test
  past the cap is named in the log and counts by its shard time, so it fails as slow; so does a
  test that fails or raises an error when run again. In each process
  `scripts/remeasure_warm_up.py` (loaded with `-p`) first runs, untimed, the cheapest other
  test of the candidate's module that uses all of the candidate's session-, package-, module-
  and class-scoped fixtures with the same instances: the same class for a class fixture, the
  same parameter for a parametrized one (never a skipped test, nor another candidate of the
  same process, which runs anyway; a flagged test in a process of its own may be one). When
  the module has no such test, it adds a no-op item beside the candidate, `test_x[warm-up]`,
  that requests only those fixtures (their dependencies and autouse fixtures come along), with
  the candidate's parameters and its markers except `skip`, `skipif` and `xfail`, and so sets
  them up; only a candidate without such fixtures runs alone. Each distinct setup gets its own
  warm-up, so two parameter sets or two classes in one process are each warmed. So no fixture
  such as `catalog_template` (a schema, about 0.7 s) or `_openapi_text` (the server's whole
  OpenAPI document, about 2 s) is ever *set up* on the candidate's clock. Teardown is not
  moved: the shared fixtures are torn down after the last item of the process, a candidate,
  and xunit1 counts that teardown in its time (small for these fixtures). Before that
  fallback, a candidate alone in its module with `openapi_schema` paid the build on every
  re-run and failed unrelated pull requests (`test_the_result_route_documents_every_format`,
  2.6 s on its own on PR #739's run; #742 had worked around the same in
  `test_access_file.py`). Moving that setup off the candidate is deliberate: in a shard it
  falls on whichever test of the worker process asks first, which is how such a test gets
  flagged at all, and the rule targets tests that are slow in themselves; the candidate's own
  function-scoped setup, call and teardown are measured in full. So a test flagged in its
  shard only for the setup it paid there is counted as noise on its re-run, and the in-shard
  measurement needs nothing more. The candidate is run up to three times (`CONFIRM_RUNS`; it
  stops after the first run below the line) and fails only if its **fastest** run takes the
  confirm line or more: `confirm_seconds` (2.5 s, in `scripts/time-budgets.toml` beside the
  threshold) scaled to the runner (see "The runner's speed" below). A margin, because a test
  that sits at the 2 s threshold passed and failed on the same code, and the fastest of
  several runs, because runner noise only adds time (on 2026-11-12 two tests that are fast
  elsewhere took 2.5 s solo on a slow runner; #550). A really slow test is slow in every run
  and still fails. The in-shard threshold stays 2 s for flagging; the log names each one
  counted as noise. A new test over
  the threshold fails with its name and both times: make it faster (a smaller fixture, no real sleep, a shared app;
  see "The CLI tests share one app" for what that once saved), or mark it slow and make another
  slow test fast. Each case of a parametrized test counts. When the number falls the check passes
  and says to lower the baseline in the same pull request:

  ```bash
  gh run download RUN_ID -p 'junit-unit-*' -D ~/.cache/lakana-junit
  uv run python scripts/time_budgets.py slow-tests --update ~/.cache/lakana-junit/*/*.xml
  ```

  `--update` never records a rise. The 206 tests marked when the ratchet started were those at
  1.5 s or more in one CI run, a margin below the threshold, one that took 2.1 s on its own in
  a later run, and the test of `scripts/select_tests.py` that reads every test file's imports; a test that drifts over 2 s unmarked, and stays there when run again, is a failure
  to fix.
- **The runner's speed (#725).** GitHub's runners differ by about 2×: in the `test (3.14)`
  logs of 20 runs on 2026-11-19, the same tests took 0.75 to 1.52 times their median
  on their own, run by run, and on a slow runner tests of 1.2 to 1.3 s locally took 2.5 to
  2.7 s and failed (#718). So before the re-runs `--confirm` times a fixed reference workload
  (`REFERENCE_WORKLOAD` in `scripts/time_budgets.py`: imports, pure Python, SQLite, JSON and
  compiling source in a fresh interpreter, like a pytest process; the fastest of five runs,
  a few seconds in all) and divides it by `reference_seconds` in `scripts/time-budgets.toml`,
  its time on a fast GitHub runner. The confirm line is `confirm_seconds` times that ratio,
  never below 1 (a fast runner keeps 2.5 s, so the rule is never looser than before) and at
  most `MAX_SPEED_RATIO` (2: a measurement gone wrong cannot switch the ratchet off). The step
  log starts with `Runner speed: the reference workload took X s here against Y s on a fast
  runner (...): ratio R, ... a test run again fails from L s`. Only the re-run's line is
  scaled: the shard times come from the `unit` jobs' runners, which this step did not
  measure, and they only pick the tests to run again. A workload that cannot run scales
  nothing and says so. Change the workload only together with `reference_seconds`. To calibrate,
  run `test (3.14)` three or four times (`gh run rerun RUN_ID --job JOB_ID`, after the first
  run ends), read the `took X s here` number of each log's `Runner speed:` line, and set
  `reference_seconds` to the lowest of them, the fastest runner seen, so that it reads about
  1.0; note the runs and the date in the comment above the value. Only `test (3.14)` runs the
  ratchet, so `test (3.11)` logs no such line.
- **Locally**, `uv run pytest -m "not slow"` skips the slow tests for a quick loop while you
  work; before a push, run the selection of "Local gates" without it.

## Flaky tests: the same-day rule

A test that fails without a code cause (it passes on a re-run of the same commit) is quarantined
**the same day**, so it stops costing every other pull request a re-run:

1. File an issue labelled `flaky` (and `area:...`, `type:test`) with the failing run's link and
   the error.
2. Mark the test `@pytest.mark.quarantine(issue=N)`, in its own small pull request.
   `scripts/pytest_selection.py` skips it in every job, with the reason "in quarantine until
   issue #N is fixed"; a quarantine without an issue number stops the run.
3. Fix it in the next batch of non-feature issues, and remove the marker in the fix's pull
   request.

The non-blocking job `quarantined tests` runs only the quarantined tests
(`LAKANA_TEST_QUARANTINED=1`, `-m ""` so a quarantined integration test runs too) and never fails
the run: its step summary says whether they passed, failed, or that none is quarantined, so a fix
can be seen to hold before the marker comes off. Run them locally the same way:

```bash
LAKANA_TEST_QUARANTINED=1 uv run pytest -m "" -rA -p no:cacheprovider
```

**Cause labels for CI failures.** So that the share of failed runs can be broken down, a pull
request whose CI failed at least once carries one label per cause: `flaky` (a test that failed
without a code cause, quarantined as above), `ci:infrastructure` (a runner, an action, a package
index, a registry or the GitHub API failed, not our code or tests), or `ci:code` (the change
itself broke a check, which is what CI is for). The same labels go on an issue filed for a
failure on `main`.

## Local gates

Local runs before a push are lean and **selected by the package graph**: the static checks always,
and the tests that import a package the change touches, read from the test files themselves. Nothing
local replaces a CI gate: **the coverage ratchet, Python 3.11 and 3.13, the oldest Polars and
macOS run only in CI** (`test (3.14)`, `test (3.11)`, the 3.11 shard's Polars step,
`test (macos)`). A local run of them is for reproducing a failure CI showed, and a coverage number
from a local run of some packages is not a ratchet result: never report it as one.

Always, whatever changed:

```bash
uv run ruff check . && uv run ruff format --check .
uv run ty check packages && uv run lint-imports
```

Then the tests the change selects (from `git diff --name-only origin/main...HEAD`):

| The change touches | Run |
|---|---|
| `packages/lakana/`, `packages/lakana-server/` or `packages/lakana-worker/` (not their `pyproject.toml`) | the changed packages' own tests, and **every test file that imports a changed package** (`lakana`, `lakana_server`, `lakana_worker`): directly, through its `conftest.py` chain, or through a local helper module it imports (`tests.server_test_helpers`, `from .near import`, a sibling `task_run_helpers.py`) |
| `packages/lakana-core/`, a shared `conftest.py` or fixture (`packages/conftest.py`, `scripts/`), `pyproject.toml`, `uv.lock`, or anything not named here | everything (`uv run pytest`) |
| only documentation (as `scripts/docs_only.py` defines it) | the documentation checks: `scripts/docs_blocks.py`, `scripts/generate_reference.py --check`, the strict build, and `pytest scripts/tests` |

So an SDK change also runs the server's CLI and SDK tests and the worker tests of `lakana.runner`;
a worker change also runs `tests/sdk/test_sdk_admin.py` of the server; a server change also runs
`scripts/tests/test_generate_reference.py`. The imports are read with `ast` on every call (about
1 s), nothing is cached, so a new import counts at once; a test file that cannot be parsed is
always selected.

`scripts/select_tests.py` prints this selection for the current branch, as the commands to run
(the static checks first). It reads the changed files from `git diff --name-only BASE...HEAD`
(`--base`, default `origin/main`), or from standard input with `-`:

```bash
uv run python -m scripts.select_tests
git diff --name-only | uv run python -m scripts.select_tests -
```

Run the tests it prints inside the capped scope, in parallel (`-n 2 --dist loadfile`, see
"Running the unit suite in parallel"). The integration tier runs locally only for the area a
change touches (`uv run pytest -m integration PATH`), as described in its sections.

## Property-based testing for the policy rewriter (M3)

Row filters and column masks (see `docs/security/threat-model.md`) work by rewriting SQL to wrap base-table
references with policy-restricted subqueries. That rewriter is exactly the kind of code
where example-based tests are dangerous: it's easy to write ten passing unit tests and still
miss the one query shape — a CTE, a `SELECT *` after a join, a view laundering a masked
column — that leaks a value the policy was supposed to hide. Coverage can hit 100% on a
rewriter that never asserts what it's supposed to guarantee.

The plan (TDD gates) is **property-based tests with hypothesis**: generate
arbitrary SQL, apply policies, and assert as invariants that masked columns never appear in
the output and filtered rows never leak — for *any* generated query, not just the ones an
author thought to write by hand. This has landed in
`packages/lakana-worker/tests/test_query_rewrite_policy_properties.py`: `hypothesis` drives a
small, deliberately adversarial set of query shapes (self-join, CTE-of-a-CTE, `UNION ALL`,
subquery wrap, and more) through `rewrite_query`, and each rewritten query is *executed*
against a real DuckDB instance via `lakana_worker.executor.execute_query` — the assertions
check actual output rows, not generated SQL text.

**Tests that run SQL live in the worker's tests.** The worker is the only package that may
import `duckdb`, and lakana-core sits below it, so a core test never imports `duckdb` or
`lakana_worker`. The tests of the rewriter that execute its output are in
`packages/lakana-worker/tests/test_query_rewrite_execution.py`; `lakana-core`'s
`test_query_rewrite.py` asserts on the rewritten SQL text. `lint-imports` reads `src` only, so the
gate for tests is `scripts/tests/test_package_test_imports.py`: it parses every file under
`packages/lakana-core/tests` (and the SDK's) and fails on an import of a module that package
must not depend on, with the file and line. Fix a failure by moving the test, not by loosening
the list.

## Mutation testing for authorization and policy (M3)

Coverage alone can't prove a permission check actually denies — 100% line coverage on
`GrantAuthorization.authorize` says nothing about whether a wrong principal, action, or
securable is refused. `packages/lakana-core/src/lakana_core/grants/authorization.py`,
`packages/lakana-core/src/lakana_core/policy/`,
`packages/lakana-core/src/lakana_core/audit/authorization.py`, and (M3f)
`packages/lakana-core/src/lakana_core/catalog/external_location.py` are mutation-tested with
`mutmut` for exactly this reason. See `docs/security/mutation-testing.md` for how to run
it, the kill-or-record-equivalent policy, and the current exit-run results.

## Polars 1 and 2

`lakana[assets]` accepts `polars>=1.30` with no upper bound, and `uv.lock` holds Polars 2.x, so
the whole suite runs on what new projects get. The oldest supported version runs as one extra
step of the `unit (3.11, 3/4)` shard in `ci.yml` (about 3 min, not sharded itself): a first command asserts that `polars.__version__`
starts with `1.30.` (the `--with` overlay shadows the locked 2.x; a change in uv's layering
must fail the step, not pass it on Polars 2), then `uv run --with 'polars==1.30.*' pytest` runs the
test files that touch Polars (the harness, the SDK data plane, the worker's Python tasks and
runners, the lock and discovery tests). **A test file that imports Polars goes into that list**,
or into `POLARS_EXCLUDED` in `scripts/tests/test_workflows.py` with a reason: a test there fails
otherwise. Run the step locally the same way, under the cap in "Memory limit".

- Code reaches what exists in one major version only through `getattr(pl, "Map", None)` (never
  an attribute access that raises on 1.30), and `lakana.runner` imports Polars lazily.
- A test that needs a type of Polars 2 (`pl.Map`) is skipped on 1.x with a reason:
  `pytest.mark.skipif(not hasattr(pl, "Map"), reason=...)`.
- A line that only Polars 1 can run has `# pragma: no cover -- Polars 1 only`: coverage is
  measured on the lock (Polars 2), and no blanket exclusion exists.
- `pa.schema(pl.Schema)` works from Polars 1.37. `Tables.create` says so (a `ValidationError`
  naming 1.37, `pyarrow.schema(...)` and a list of columns) instead of showing pyarrow's
  `TypeError`; a test of that message monkeypatches `pl.Schema.__arrow_c_schema__` so it runs on
  both majors, and the two tests that create a table from a `pl.Schema` are skipped before 1.37.
  The version they compare is `pl.__version__`: with the overlay two `polars` distributions are
  visible to `importlib.metadata`, and `version("polars")` may name the locked one.

## Object-store emulators

`packages/lakana-worker/tests/test_ingest_stores_integration.py` (`integration`) reads files
through `lakana.ingest.files` from three emulators, each over TLS, and through the real egress
proxy. It lives in the worker's tests because it builds the trust bundle with
`lakana_worker.trust.build_trust_bundle` and runs `EgressServer`, which `packages/lakana` tests
may not import.

| Store | Image (tag and digest) | Licence | Base image | TLS |
|---|---|---|---|---|
| S3 | `chrislusf/seaweedfs:4.47@sha256:ce9e796f1fe6f06968f4c04bdaf8f678dad9c8acdfef3d244133d71bfa6bf882` | Apache-2.0 | Alpine 3.24 | `-s3.cert.file` / `-s3.key.file` |
| GCS | `fsouza/fake-gcs-server:1.56.0@sha256:4957e37e252c8720cf861eb14e0b5aa671845e833c49285a45465ec7fa24e981` | BSD-2-Clause | Alpine 3.24 | `-scheme https -cert-location -private-key-location` |
| Azure | `mcr.microsoft.com/azure-storage/azurite:3.37.0@sha256:830430c1da1a2d537e08f3e6764dd1f5ae00cf0346bcaf625b968ec3f0971fd5` | MIT | Alpine 3.23 with Node 22 | `--cert` / `--key` |

The Alpine base images hold BusyBox (GPL-2.0). The images are test containers only: pulled by
the integration tier, never built into, copied into or shipped with a Lakana artifact, so the
licence rule for dependencies does not reach them (the same judgement as for the `postgres`
image of the integration tier). Change a tag only together with its digest.

How the module sets them up:

- **Certificates.** One test authority (`tls_fixture.make_authority`) signs one leaf for
  `127.0.0.1` and `localhost` (`issue_leaf`); the leaf and its key go into each container with
  `with_copy_into_container` at `/certs`. The task trusts the authority only through
  `LAKANA_CA_BUNDLE`, which points at `build_trust_bundle(authority.ca_file)` (Mozilla's roots
  plus the authority), the way production trusts a private-CA store. Every URL uses
  `127.0.0.1`, never `localhost` (which may resolve to `::1`).
- **SeaweedFS** gets one identity (as `SeaweedFsContainer` does) and checks signatures, so the
  wrong-secret case runs against it. The bucket is made with the `minio` client over TLS.
- **fake-gcs-server** serves its XML API, where obstore lists and reads, only on its
  `-public-host`, which must equal the `Host` the client sends: the module picks a free host
  port first, binds it, and passes `127.0.0.1:<port>`. It refuses obstore's upload of a key
  holding `/`, so the module uploads through its JSON media upload. It accepts any key, so GCS
  has no wrong-secret case. `_stores.GCS_BASE_URL` is patched to the emulator.
- **Azurite** runs the blob service only. obstore cannot create a container, so the module sends
  one Shared Key request (`PUT ?restype=container`, signed with Azurite's published development
  key) and makes an account SAS for the SAS case. It checks Shared Key signatures, so the
  wrong-secret case runs against it too. `_stores.AZURE_ENDPOINT` is patched to
  `https://127.0.0.1:<port>/devstoreaccount1` (path style).
- **The egress proxy** is `EgressServer` in the test process, allowed to reach loopback
  (`reachable`) and given a routing table with one attached network (it refuses every direct
  connection without one). A token minted for the emulator's port reads; a token for another
  port is refused.

The module takes about 45 seconds once the images are pulled. obstore retries a failed
connection ten times within about 2 to 5 seconds before it reports it, which is most of the time
of the certificate and proxy-refusal cases.

## Database images

`packages/lakana-worker/tests/test_ingest_sql_integration.py` (`integration`, Linux only) reads
databases through `lakana.ingest.sql` over TLS and through the real relay
(`lakana_worker.launcher.relay.serve` in the test process, on `127.0.0.6` inside its plan's
`127.0.0.0/29`, so libpq's `hostaddr` dial from `127.0.0.1` is on the task's network). It lives in
the worker's tests because it runs the relay and builds the trust bundle, which `packages/lakana`
tests may not import.

| Database | Image (tag and digest) | Licence | Base image | TLS |
|---|---|---|---|---|
| PostgreSQL | `postgres:18.6@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6` | PostgreSQL Licence | Debian 13 | `-c ssl=on -c ssl_cert_file=... -c ssl_key_file=...` |
| MariaDB (11.8, an LTS release) | `mariadb:11.8.9@sha256:f16d7a344413e519dff518f27bdb221388f84b0d274c97c4fb5f2cb0dc1b34fa` | GPL-2.0 (the server) | Ubuntu 24.04 | `--ssl-cert=... --ssl-key=... --require-secure-transport=ON` |

The images are test containers only, never built into, copied into or shipped with a Lakana
artifact (the judgement of "Object-store emulators" above); that is why the GPL-2.0 MariaDB
server may be one. The client Lakana ships for it is PyMySQL (MIT). Change a tag only together with its
digest.

- **Certificates.** One test authority signs a leaf for `db.test`; the entry point installs it
  with the key as uid 999, mode 0600 (PostgreSQL refuses a key it does not own or others may
  read, and a file copied in is root's), then starts the image's own entry point.
- **Readiness.** Each image's first-run setup starts a server without TCP; the module waits for
  the log line of the TCP listener, then for `pg_isready` over TCP (PostgreSQL) or
  `mariadb-admin ping` on the socket (MariaDB, whose `require_secure_transport` refuses a plain
  TCP client).
- **MariaDB** requires TLS, so `tls: disable` is refused by the server; its relay port is
  15002 on the same `127.0.0.6`.
- **The relay** gets a routing table that lists only `10.9.0.0/24` as attached (loopback is not
  attached, so it is not refused) and `reachable={127.0.0.0/8}`; `lakana-relay` resolves to
  `127.0.0.6` through a patched `socket.gethostbyname`.

The Docker E2E case `test_sql_read_reaches_postgresql_through_the_relay_with_verify_full` in
`test_docker_task_egress_integration.py` runs the same image on the sources' network (alias
`db.lakana.test`) and adds its address to `relay_reachable` for that case only.

**SQL Server (the recipe of #56).** `test_docker_proprietary_db_integration.py` (`docker`,
`integration`, `slow`) runs the two assets of `docs/examples/proprietary-db/project/` under the
worker's `DockerRunner`, through the relay of a `tcp` connection, against
`mcr.microsoft.com/mssql/server:2022-latest@sha256:cf8f0ab2ebe61feec2eed2629061a07d247f791c2a8ddda74a4ac9f06a291718`
(SQL Server 2022 Developer edition, build 16.0.4305.5, Ubuntu 22.04, linux/amd64 only, about
1.7 GB; the module skips on another architecture). Its terms are *Microsoft Software License
Terms, Microsoft SQL Server 2022 Developer, Express, Evaluation* (October 21, 2022), section 1.a.i:
"You may install and use copies of the software on any device, including third party shared
devices, to design, develop, test and demonstrate your programs. You may not use the software in
a production environment." Testing our recipe on CI runners is that use, so the module sets
`ACCEPT_EULA=Y` (maintainer decision of 2026-10-27; the check is on #56). Section 5 forbids
publishing benchmark results without Microsoft's approval: no timings of it anywhere. Like the
other images it is a test container only, never part of a Lakana artifact.

- It reuses the egress module's stack (`sources = egress_cases.sources`, `worker_side` likewise:
  pytest finds a fixture by the name it is bound to in the module) and adds the server's /32 to
  `relay_reachable` per case. It pulls the image by digest before `create`.
- `mssql.conf` (put in as uid 10001 with the module authority's leaf for `erp-db.lakana.test`)
  sets `forceencryption = 1` and TLS 1.2. The second alias `erp-db-2.lakana.test` is a name the
  certificate does not hold. Passwords are generated per run; `reader.sql` of the example makes
  the read-only login, run by `sqlcmd` with the password from the environment.
- The task's environment is the wheelhouse's `lakana[assets]` plus the example's driver and its
  tree from the public index, installed from `_DRIVER_REQUIREMENTS` with `--require-hashes
  --no-deps --only-binary=:all:` (the SHA-256 of each linux x86-64 wheel; cffi's cover CPython
  3.11 to 3.14, so another Python fails the install, naming the hash it lacks). The case needs the
  network and `LAKANA_TEST_WHEELHOUSE`; the fixture checks the wheelhouse before it pulls the
  image. `test_the_driver_pins_are_the_examples` keeps the direct pins equal to the example's
  `pyproject.toml`.
- Each case runs a probe asset that loads the example's file next to it and calls its function:
  the full read; the incremental read by `rowversion` below `MIN_ACTIVE_ROWVERSION()` and its
  second run from the first run's largest `row_version`, after an order with a smaller id was
  inserted (a late commit) and another changed; a refused login; a closed port (the relay's line);
  no trust bundle; a bundle of another authority; and the other name. `python-tds` 1.17.1 with
  pyOpenSSL 26.2 or newer refuses a name that is not the certificate's CN with an
  `AttributeError` (the fix is merged upstream, not released); the case asserts that text, so
  bumping the pin to a release with the fix turns it red, and the page changes with it.
- `docs/examples/proprietary-db/run.sh` (the docs-examples job, no Docker) creates and tests the
  connection, locks the project with the driver, deploys it, and checks that the asset refuses to
  connect without a trust bundle.

## Python assets in the two tiers (M5c2a)

**Unit tier.** `uv` is a scripted stand-in (`packages/lakana-worker/tests/test_venvs.py`), the
task process is a real subprocess of the test interpreter running a scripted harness
(`test_runners.py`), and the worker's Python path runs against a fake `Runner` whose script
uses the real database, token service, query service and catalog (`test_python_tasks.py`).
`test_discovery_harness_parity.py` puts one set of asset sources through deploy discovery and
through the harness's function selection, so the two cannot drift apart. The adversarial token
cases run over `TestClient` with real `GrantAuthorization`
(`packages/lakana-server/tests/api/platform/test_bound_task_tokens.py`). All of it counts
toward the coverage ratchet. `test_index_credentials.py` and `test_venvs_private_index.py` are the
exception to the scripted `uv`: they run the real `uv sync` against two package indexes on loopback
(`index_fixture.py`, served by the test process) and install one package of under 1 KiB, so they
need `uv` (which `uv run pytest` provides in the `UV` variable) and no network. They pin what uv
does with a credential; if they fail after an upgrade of uv, the design of the package index support has to be looked
at again, not the test.

**Integration tier.** Two modules in `packages/lakana-server/tests/`, both with a real HTTP
server (uvicorn on a loopback port, `python_asset_helpers.serving`) on Postgres and SeaweedFS:

- `test_bound_tokens_integration.py` needs no packages: it writes a running task attempt into
  the database and mints its token (`python_asset_helpers.bound_attempt`), then races uploads
  against reclaims and cancels, and two creates of one table against each other.
- `test_python_assets_integration.py` runs the real `uv` and the real harness in one in-process
  worker. Its packages come from a wheel directory, never from an index:
  `scripts/build_wheelhouse.sh DIR` builds the `lakana` wheel of the working tree and downloads
  the locked wheels of `lakana[assets]`, checked against the hashes in `uv.lock`
  (`--require-hashes`). That is the only step that needs the network, and only for wheels the
  directory does not hold yet. CI runs this module in a job of its own, `integration (python
  assets)`, beside the tests that install no packages: it restores the directory from a cache keyed
  on `uv.lock` and `.python-version`, runs the script as its own step, and runs this module with
  `LAKANA_TEST_WHEELHOUSE` set; the job has `timeout-minutes: 30` and a budget of 15 minutes.
  `integration (docker)` repeats the same cache and build steps and sets the variable too, for the
  Docker module's end-to-end ingestion asset
  (`test_an_ingestion_asset_reaches_its_declared_connections_and_nothing_else`). Each test project commits
  a copy as `wheelhouse/` and sets `[tool.uv] no-index = true`, `find-links = ["wheelhouse"]`;
  the worker under test has its proxy pointed at the discard port, so a build that reached for
  an index would fail. The directory is never committed to this repository.

```bash
bash scripts/build_wheelhouse.sh ~/lakana-wheelhouse          # once, and after changing the SDK
LAKANA_TEST_WHEELHOUSE=~/lakana-wheelhouse uv run pytest -m integration
```

Without `LAKANA_TEST_WHEELHOUSE` the Python-asset module is **skipped** with a message that
says how to build the directory (in CI, where `GITHUB_ACTIONS=true`, it fails instead: there the
step is part of the job). The harness under test is the one in the wheel, so rebuild the
directory after changing `packages/lakana`. No test in the module waits without a bound: a
query waits 30 s; a run, and every drain of the worker's queues (`_Stack.work`), has a 240 s
deadline that fails the test; the deadline is checked between claimed items, and one item is
bounded by the product (asset and query timeouts of 90 s, a venv build of 180 s).

**Private package indexes (M5c2c).** Six tests of `test_python_assets_integration.py`
(`-k "private or secret_stops or wrong_password or removed_index or without_a_wheel or another_host"`) deploy a project
that takes one package from an index with a password, served on loopback by the test process
(`packages/lakana-worker/tests/index_fixture.py`, loaded by path and registered in `sys.modules`
before it runs). Such a project cannot use `no-index`, which also switches off an explicit index: it
uses a flat index with `default = true` for the wheel directory and the explicit private one, and
locks with the developer's credential in `UV_INDEX_TEAM_USERNAME` and `UV_INDEX_TEAM_PASSWORD`.
Each test starts its own index, because uv's download cache would otherwise serve the wheel without
asking (the fixture marks its files as fresh, as real indexes do). The test server and its worker
have a secrets master key (`python_asset_helpers.MASTER_KEY`). The messages the how-to page
`private-package-index.md` quotes as output are the ones these tests assert. The page's example `pyproject.toml`
(the `lakana` index from `wheelhouse` plus the `team` index, PyPI as the default for the rest)
cannot be the integration project: that one cannot reach PyPI, so it uses a flat default index. The
page's file was locked once against the fixture by hand with the real uv (a probe, not a test);
change the page's example only together with such a probe. The page's registering, rotating and
clearing of an index run in the `getting-started` docs example.

The helpers both modules share with `test_run_engine_integration.py` (unique names, git
commits, waiting for a Postgres lock waiter) live in `tests/integration_helpers.py`.

**The artifact store (M5c2b).** `test_artifacts_integration.py` runs the store against Postgres
and SeaweedFS over real HTTP and installs nothing, so it needs no wheel directory (about 30 s,
one container pair): a file over one multipart part (12 MiB, the largest file any test writes),
the commit guard's race with a reclaim, a dead attempt's upload cleared by the next claim
(`python_asset_helpers.reclaim`; a blob request held open mid-body shows the "upload gone" answer
deterministically), the bound token, the same refusal for an existing and a missing artifact on
every route, retention, deletion, the sweep, a hand-made manifest that breaks a path rule or a
limit, the audit of downloads, and the protected environment. `test_python_assets_integration.py`
adds `test_train_then_predict` (a model version per run, used in the same run, with lineage and
the CLI's `runs show`) and the adversarial cases that need the real harness: a forged success,
a hostile return value, a consumer whose run principal may not read the artifact, a consumer
deployed before its producer, an attempt killed after its commit (the `_Hold` middleware holds
the commit's answer until the asset's timeout kills the process) and a version pruned between
resolution and download. They share one deployed project (the `artifacts` fixture) and pick
their assets with `select=`. `scripts/python_asset_project.sh PROJECT WHEELHOUSE PYTHON artifact`
adds a producer and a consumer for the Compose smoke test, which stores, lists and downloads
an artifact (and downloads it again after the restart), and for the five-minute test, which runs
the same pair in local mode. Run the artifact tests like every integration test, in a capped scope:

```bash
systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 timeout 900 \
  uv run pytest -m integration packages/lakana-server/tests/test_artifacts_integration.py
systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 \
  env LAKANA_TEST_WHEELHOUSE=~/lakana-wheelhouse timeout 900 \
  uv run pytest -m integration packages/lakana-server/tests/test_python_assets_integration.py -k "train or forged or hostile or consumer or killed or pruned or refuses"
```

`scripts/tests/test_imports_in_a_fresh_interpreter.py` imports alone in a new process each entry
point (every `[project.scripts]` target of the four packages, plus the modules started with
`python -m`: `lakana.runner` and `lakana.cli.deploy.setup`) and, in one process that purges
`lakana*` from `sys.modules` before each import, every module of the four packages except the
migrations. A package-level import cycle shows only when its first member is imported by itself
(the worker's console script could not start because of one), and every other test imports the
catalog or a web application first. It lives under `scripts/` because it names modules of all
four packages; it only starts subprocesses, so import-linter has nothing to object to. A new
`python -m` entry point goes into its `_PYTHON_M_MODULES` list.

## Docker isolation tests (M7b)

The tests marked `docker` (also `integration`) start real containers through the launcher in
the test process: `packages/lakana-worker/tests/docker_stack.py` builds the task image of the
running Python from `Dockerfile.task` (its own context, with `task_build.py` and the connection
check `_check.py`, plus `dist/check-requirements.txt` exported from `uv.lock` for the check
environment, so the build needs the network: the Docker SDK reads only the root `.dockerignore`), creates a work volume, a server stand-in labelled as the stack's `server`
service and one environment with a stub harness that runs the attempt's `probe.py`. Every
object carries `lakana-test=1` and its stack's `lakana-test-stack=<name>`, and is removed, also
after a failure; a test that needs a launcher of its own (the one that stops it) brings up a
second stack without touching the first. One test per property of the exit criterion; each
probe is the task, among them that a task sees no other attempt in the shared work volume (a
sibling attempt's canary files stay out of reach and the mounts' roots are the attempt's own
subpaths) and that a task stopped through `stop`, ignoring SIGTERM, leaves no process and no
labelled object. `test_docker_isolation_integration.py` also holds the real-engine cases the
launcher's reviews asked for: no IPv6 address and no reach to the host's link-local addresses,
no resolution of outside names (a forwarded query would be an exfiltration channel), the
gateway address of the task's own /29 answering nothing (the isolated gateway mode), a subpath
that is a link out of the volume, no anonymous volume, no setuid or setgid file in the image, a
launcher that stops removing its running tasks, 40 tasks at once, the ingestion-root probe on a
pipe and on a directory the task user cannot list, and the uv caches of real offline builds (a
project locked by the host's `uv` with two wheels in its repository): a build that may run
package code gets a private cache that is gone when it ends, two wheels-only builds share one
cache yet each gets its own repository's wheel at the same path, and no installed code runs
while uv builds.

`test_docker_trust_integration.py` proves the trust bundle (#292) end to end. Its module fixture
makes a private authority (`tls_fixture.make_authority`, a leaf for `index.lakana.test` with
`issue_leaf`), starts a TLS index container on a labelled network `<stack>-index` (the task
image runs `index_fixture.py`, copied in, on `0.0.0.0:8443`, with that network alias), and the
egress service on that network through `docker_stack.start_egress(stack, network=…,
reachable=<its subnet>)`: the proxy otherwise refuses an address on a network it is attached to,
and `EgressSettings.reachable` is not a setting, so the entry sets it in code. The project
(`index_fixture.project_files` plus the stub `lakana` wheel) is locked on the host against the
loopback index and then rewritten to `https://index.lakana.test:8443`. The cases: a build with
the bundle (`Stack.build(..., targets=..., trust=True)`) completes; one without fails with uv's
`invalid peer certificate`, which `trust.certificate_hint` recognises (it is not wheels-only, so
the shared cache of the trusting build cannot spare it the connection); a task's Python trusts a
TLS server of the authority on the container's own loopback with the mount and refuses it
without, and sees `SSL_CERT_FILE` and `LAKANA_CA_BUNDLE` only with it; and `/trust` refuses a
write with `EROFS` and holds `trust.pem` alone. `tls_fixture` leaves carry the issuer's key
identifier, which Python 3.13's strict default context requires. `start_egress` also serves
`test_docker_egress_integration.py`; `stack()` removes the networks a test made with its labels.

`start_egress` runs the proxy in `build_egress_image`: the task image plus this checkout's
launcher package at `/opt/lakana-launcher`, where the real egress image has it, because a task's
relay runs `RELAY_COMMAND` in the egress container's image. The container carries the stack's
Compose labels for `egress` (`_service_container`), and the network it is created on carries only
test labels, so it is the outward network (`_outward_network`). The proxy reads a copy of the
launcher's key and `refused.json`; `start_egress` wraps the launcher's `_replace_refused`, so every
time the launcher writes the refused ranges (at each network open) the copy is replaced too, with
a PAX nanosecond mtime: the proxy re-reads on a change of inode, mtime or size, and Docker may
reuse the inode for a list of the same length. A network a test makes after the launcher started
thus has its gateway refused from the next run on, as in production. `start_egress` returns only
once the proxy accepts connections (`wait_until_listening`: a connection from inside the container
to port 3128, every 0.2 s, at most 60 s, then a failure with the container's log): `docker start`
returns before the proxy's Python listens, about half a second later, and a build that connected
in that gap got `Connection refused` (#319).

`test_docker_task_egress_integration.py` (all `slow`) proves task egress (#308): a source container
on a labelled network `<stack>-sources` serves TLS on 8443 (a private authority's leaf for
`target.lakana.test`), a stand-in PostgreSQL that answers the first client message on 5432, and an
echo on 9000, and a neighbour (`postgres.lakana.test`, a stand-in for a stack service) on the same
network; the egress service is created on that network with `reachable=<the source's /32>`, and
the module runs under `Stack.configured(relay_reachable=(<the same /32>,))`, the relay's test hook
(never a setting), so the rest of the network stays attached and refused. `Stack.run(..., egress=..., relay=...)` passes the routes the worker would send. The
cases: a declared HTTPS target through `LAKANA_EGRESS_PROXY` with the trust bundle (and no other
proxy variable); the first PostgreSQL message and a mebibyte of every byte value through the
relay; an undeclared host and port (the whole 403 with the task hint); the metadata address, the
stack's server on the task's network (also in the task range), the neighbour (attached and in no
refused range, so only the attached-network rule refuses it) and the default bridge's gateway
(read from Docker), each refused by the proxy
(whole text) and the relay (the whole `lakana-relay: refused ...` line in the run's log); a range
of `egress_refused` naming the source's network, refused through both, and the same target reached
without it (a refused range wins over `reachable`); another task's token (`this token belongs to
another client network`, its container's environment read with `Stack.container`); a client on
the outward network, as root with `NET_ADMIN`, routing the task's /29 through the relay (refused
before the relay sees it on Engine 28; the relay's own outward address answers nothing); the
source network's gateway, made after the launcher started, which the proxy's own copy of
`refused.json` now holds; and teardown after exit, stop and
timeout (nothing labelled with the run's id, no token expiry, the subnet resting).
`test_connection_test_outcomes` (#308, M6 PR 5d), per route class, runs the worker's own path:
`ConnectionTestLoop._run_check` with a `DockerCheck` whose client copies the attempt directory into
the work volume and sends the check to the module's launcher over its socket (the `worker_side`
fixture serves it), with the plan of `python_tasks.connection_plan`. Proxy: `ok` with the trust
bundle, `tls` without it, `dns` for a name the proxy cannot resolve, `refused` for a closed port,
`not_allowed` for a token that does not name the host. Relay: `ok` for PostgreSQL with
`tls: disable` (only because the relay reported `connected`), `tls` (not offered) without it,
`refused` to a closed port, and `timeout` for a blackholed port (the source's port 7000 accepts
one connection it never takes, so later SYNs are dropped; the check says `ok` to the relay at once,
the launcher waits for the relay's verdict, and the outcome is never `ok`). tcp: `ok`. Mount:
`root_unavailable` through the launcher's refusal of a root it does not have. After each, nothing
is labelled with the check's id.
`test_an_ingestion_asset_reaches_its_declared_connections_and_nothing_else` (#308's "Done when")
installs the wheelhouse's real `lakana[assets]` into an environment volume (it needs
`LAKANA_TEST_WHEELHOUSE`, skipped without it, failed with `LAKANA_TEST_DOCKER=1`), serves the two
connections' answers from the stand-in server's task route, and runs an asset through the
worker's `DockerRunner` with the routes of `connection_plan`: `connections.get` gives the proxy
URL and the relay address, httpx reaches the HTTPS source through the proxy with the trust bundle,
a socket gets the PostgreSQL answer through the relay, and an undeclared host gets the proxy's 403.

The test's Docker client and the launcher use one socket: `DOCKER_HOST` when it is
`unix://<path>`, else `/var/run/docker.sock`; another `DOCKER_HOST` skips them (fails with
`LAKANA_TEST_DOCKER=1`), since the launcher speaks to Docker only over a Unix socket.
They need Docker Engine 28 or later and skip without Docker, unless `LAKANA_TEST_DOCKER=1`
(CI's "Docker isolation tests" step) makes that a failure; the build test also needs `uv` on
the `PATH`. GitHub's `ubuntu-latest` has Docker 28 or later; a self-hosted `CI_RUNNER` needs it
too. They pass on Docker installed as a snap, where `no-new-privileges` is detected as off, if
the base temp directory is under the home directory: a snap's Docker cannot see the host's
`/tmp`, so the ingestion-root probe would find no directory there. Run them with
`--basetemp ~/.cache/<name>-tmp`, never in the background, and check what is left afterwards
(the containers the launcher made run outside the test's memory scope; the 40-task test holds
about 2.5 GB at its peak):

```bash
systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0 timeout 900 \
  uv run pytest packages/lakana-worker/tests/test_docker_isolation_integration.py -m docker \
  --basetemp ~/.cache/lakana-docker-tmp -q -p no:cacheprovider
rm -rf ~/.cache/lakana-docker-tmp
docker ps -a --filter label=lakana-test=1 -q; docker volume ls --filter label=lakana-test=1 -q
docker network ls --filter label=lakana-test=1 -q
```
