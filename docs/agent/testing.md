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
`from scripts.test_support.egress_helpers import ...`. Never add another package's `tests/`
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

CI runs the unit suite in **three shards per Python version**, side by side: the jobs
`unit (3.11, 1/3)` to `unit (3.14, 3/3)`. `LAKANA_TEST_SHARD=I/N` (read by
`scripts/pytest_selection.py`, which both `conftest.py` files load) keeps shard I of N: whole test
files, spread over the shards by the seconds in `scripts/test-durations.json`, the longest first,
each to the shard with the least time so far; a file without an entry counts one second. Every
shard and every xdist worker computes the same split from the same collection, so the shards
together run every test once. When the shards drift apart (a budget failure shows it), refresh the
file from the junit artifacts of a green run (`junit-unit-*`, `junit-integration-*`):

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
whole suite the way the 3.14 shards do together, inside the capped scope, use `-n 2`:

```bash
systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 timeout 900 \
  uv run pytest -n 2 --dist loadfile --cov --cov-report=json -q -p no:cacheprovider
```

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

### The server tests copy one catalog and skip fsync (#327)

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
shards, `integration` and its jobs, `five-minute`, `compose-smoke`, `docs-examples`, the macOS
jobs, `quarantined tests` and `static`. The `test` job still
runs on both Python versions, but only `scripts/tests`, which holds every test that reads the
documentation (the legacy-name check among them). `docs` (the block check, the strict build and the
check of the built site), `changes`, `conventional`, `secrets`, `cla` and `time budgets` run as always. For a
documentation-only change the `docs` job and the `test` job's `scripts/tests` together are the
guard against a broken page, so `docs` has no condition. `scripts/tests/test_docs_site.py`,
which reads `mkdocs.yml` (the navigation, `docs_dir`), is among those `scripts/tests`, so a
docs-only change to the navigation is checked by it and by the strict build. Because `lint` and `static` are skipped for such a change,
the same file fails on a `hooks:` key or a plugin in `mkdocs.yml` (code run at build time) until it
is listed in a pull request that is not docs-only. A push to `main` runs everything unless
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
| Pull request, **draft** | `lint`, `docs`, `docs-examples`, the 3.14 `unit` shards and `test (3.14)` (with coverage and the ratchets), `quarantined tests`, `time budgets` |
| Pull request, **ready for review** (also the moment it leaves draft) | everything: the above plus the 3.11 shards and `test (3.11)`, the macOS shards and `test (macos)` (they test only if the pull request touches `packages/`), the `integration` jobs, `five-minute`, `compose-smoke` |
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
  it proves that, `unit`, `test`, the `integration` jobs, `five-minute`, `compose-smoke`,
  `docs-examples` and `quarantined tests` skip. It says "already tested" only if the commit has exactly one pull request, merged with
  this very commit into the repository's default branch, whose base was never changed (no
  `base_ref_changed` or `base_ref_force_pushed` event: a run made against another branch
  proves nothing about `main`) and whose head commit no other pull request shares (runs are
  found by head commit, so another pull request's run would be taken for this one's), one
  parent that the head contains, the head's tree equals this tree, and
  the latest `pull_request` run of `ci.yml` for the head completed with `test (3.11)`,
  `test (3.14)`, `integration`, `five-minute`, `compose-smoke` and `docs-examples` all
  `success` (a `skipped` job, as on a draft or a documentation-only change, is a no; "latest"
  is by run number, so a failed re-run of an older run does not override a newer green run).
  The first lookup of the pull request is retried once after 10 s, for the API's lag right after
  a merge. Every
  doubt, API error, timeout, missing `gh`, or truncated page says "not tested" and runs
  everything; the job log has one line with the reason. A pull request event never skips this
  way. If you rename one of the six jobs, change `REQUIRED_JOBS` in the script too: until then
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
  "Did the bundle change?" step. A `unit` shard has a 25-minute timeout, `test` 15 minutes.
- **Coverage** is measured on 3.14 only, by the three 3.14 shards, and combined in exactly one
  job, `test (3.14)` (see the coverage ratchet below). That job uploads the combined report,
  `coverage.json`, as the artifact `coverage-report` (kept 7 days), also when the ratchet fails,
  and its log lists every file below 100 % with the missed lines.
- **Time budgets** are checked by `time budgets`, the last job, and by the slow-test ratchet in
  `test (3.14)` (see "Time budgets" below).
- **`quarantined tests`** runs the tests in quarantine and never fails the run (see "Flaky tests:
  the same-day rule").
- **Duplication** is measured by the `lint` job: `jscpd`, pinned in `tools/jscpd`, scans
  `packages/*/src` (not the tests) and `scripts/duplication_ratchet.py` fails when the duplicated
  lines or the clones rise (see "The duplication ratchet" below).
- **`test (macos)`** (job `test-macos`) is the macOS check: Lakana's local mode supports macOS
  (Python 3.13 and later), and the three shards it collects, `unit (macos, 1/3)` to `(macos, 3/3)`
  (job `unit-macos`), run what a macOS user runs, on GitHub's `macos-latest`
  (Apple Silicon, 3 cores, 7 GB) with Python 3.14. They run the `packages/` unit suite (not
  `scripts/`, which is Linux tooling whose tests need GNU tools) with
  `pytest packages -m "not integration" -n 2`, without coverage (the ratchet stays on the Linux 3.14 job), and
  the third shard runs `scripts/run_docs_examples.sh getting-started`, which ends with `lakana server reset --local`.
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
  pull-request jobs cannot. A failure is a failed scheduled run (GitHub notifies the maintainer;
  the job has `contents: read` only and files no issue); the step summary lists the resolved
  versions (`pip freeze`) so the run names the release that broke it. It is outside `ci.yml` and
  its `changes` gating, and on GitHub's runner whatever `CI_RUNNER` says.
- **`security-deep.yml`** runs semgrep and Trivy weekly. The semgrep scan (a pinned image, no write
  scope, no token in the workspace) keeps its SARIF as an artifact; `semgrep-upload` alone has
  `security-events: write` and does nothing but upload it to code scanning. Dependabot does not
  read a workflow's `container:` image: bump the semgrep version and digest by hand.
- **`codeql.yml`** runs CodeQL for Python on pull requests, on a push to `main` and weekly, on a
  GitHub-hosted runner, with `security-events: write` on its one job. It is CodeQL's *advanced*
  setup: switching on *default setup* in the repository's code-scanning settings would make its
  uploads fail, so leave that toggle off.
- **Token permissions.** No workflow has a write permission at its top level: each job names its
  own (`cla.yml` and `security-deep.yml` included). `scripts/tests/test_workflows.py` fails on one.
- **`release.yml`** is described in `docs/agent/release.md`; it also attaches the SBOM to each
  GitHub release, and publishes the documentation site on GitHub Pages (`pages` and
  `pages-deploy`; see "The documentation site on GitHub Pages" there).

## The documentation site

The user documentation is a site under `docs/site/`, built by Zensical from `mkdocs.yml`. Every
code block on a page is synced from a tested file, parsed, marked as output, or marked as not
tested and counted (below). Everything here is for someone adding or changing a page.

### Pages

One directory per kind, each with an `index.md` that introduces it:

| Directory | Kind | A page is |
|---|---|---|
| `docs/site/tutorials/` | Tutorial | learning by doing, start to finish, one working result |
| `docs/site/how-to/` | How-to guide | one task, the shortest path |
| `docs/site/reference/` | Reference | exact facts: every option, default and limit |
| `docs/site/explanation/` | Explanation | why it works this way |

Three reference files are generated, never edited by hand: `reference/cli.md` from the Typer app,
`reference/settings.md` from the settings models of `lakana-core` and `lakana-server` (the
description of a variable is its entry in the model's `Parameters` docstring section, so document
a new setting there), and `reference/openapi.json`, the OpenAPI document of the platform REST API
(`docs/agent/release.md`, "The REST API contract"). After a command, an option, a setting or a
route changes, run
`uv run python scripts/generate_reference.py` and commit the pages; `--check` fails, naming the
stale page, and runs in CI's `docs` job and in `scripts/tests/test_generate_reference.py`.

A new page must be listed under `nav` in `mkdocs.yml`, and every entry must be a page;
`scripts/tests/test_docs_site.py` fails otherwise. Link to other pages with relative `.md` links;
the strict build fails on a broken link or anchor.

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
  "Private package indexes" paragraph below are the proof. The upgrade note of the next release rose it from 48 to 52 (two blocks with placeholders for names and ids, and the recovery `SELECT` and `UPDATE` of "Delegation needs the ADMIN role" in `upgrades.md`, which read and change a real database); its other commands run in `getting-started/run.sh`. "Why is my query or task waiting?" (M5c3b) rose it from 53 to 54 for `lakana query status QUERY_ID`, which needs the id of a query that is still queued; the tests of the wait line in `test_queries.py` assert the line the page shows. The upgrade note "A secret is bound to one use" rose it from 52 to 53 (placeholders for names; the commands run in `bind-a-secret/run.sh`). "Verify a download" in `deploy-with-compose.md` rose it from 53 to 54 (#130): its `pip download` and `gh attestation verify` need the network, the GitHub CLI and a published, attested release, which the example harness cannot provide. Sorting the pages (#55) lowered it from 55 to 48 by testing seven blocks (the new example `projects`, `uv lock` from `train-and-predict`, and the `*lakana-env` block, which now shows its anchor and is parsed) and giving the other 21 "moved as is" blocks a real reason; the reason `moved as is` is refused by `scripts/tests/test_docs_site.py`. The operator's guide (#57) rose it from 48 to 51 (the single backup procedure of `deploy-with-compose.md` replaced two blocks, so 50): the three blocks of its recipe "One instance per client" (two Compose stacks on one host, `docker compose ls`, the upgrade of one) need Docker and two complete stacks, which the example harness cannot provide. The health page (#45) rose it from 50 to 53 with three blocks that ask Docker about a running stack (`docker compose ps --all`, the disk use, the log filter that removes secrets); the health route, the queue listings and the version run in `getting-started/run.sh`. The restore (#200) lowered it to 52: the backup block is synced from `docs/examples/compose-restore.sh`, which `compose-smoke` runs. Lowering the ceiling when blocks become tested is still the expected direction.


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
- **The checker compares.** `scripts/docs_blocks.py --captured DIR` (or the same variable)
  compares each tied block with its file after both are masked (below); a mismatch names the
  page and line and prints a diff of the masked texts. Without a capture it checks only that the
  region exists in `run.sh` and says that the comparison was skipped. CI's `docs-examples` job
  runs the examples with the variable set, compares after them, and uploads the captures as the
  artifact `docs-captures` even when a step failed.
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
| `timestamp` | ISO 8601 date and time, with or without zone | `<timestamp>` |
| `hash` | 40 to 64 hexadecimal digits: commits, SHA-256 digests | `<hash>` |
| `token` | `lakana_pat_`, `lakana_ci_` and `lakana_task_` tokens | `lakana_pat_<key id>_<secret>` |
| `port` | the port after `localhost`, `127.0.0.1`, `0.0.0.0`, `[::1]` | `host:<port>` |
| `path` | absolute paths under `/tmp`, `/var/tmp`, `/var/folders`, `/home`, `/Users`, `/root`, `/run/user` | `<path>` |
| `version` | `1.2.3`, with `.dev4`, `rc1`, `+g1a2b3c` | `<version>` |
| `duration` | a number with `ms`, `s`, `sec`, `seconds`, `min`, `minutes` | `<duration>` |
| `size` | a number with `KiB`, `MiB`, `GiB`, `KB`, `MB`, `GB` | `<size>` |

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
   `start.sh`. The runner appends `--port <free port>`.
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
uv run python scripts/generate_reference.py     # rewrite the CLI, settings and OpenAPI references (--check: fail when stale)
uv run --group docs zensical build --strict     # build into site-build/ (a traceback after the message is normal on failure)
uv run python scripts/check_built_site.py site-build   # external resources, missing files and URLs outside /lakana/
uv run --group docs zensical serve              # preview on http://localhost:8000
uv run bash scripts/run_docs_examples.sh        # every example, or name one: ... getting-started
uv run pytest scripts/tests                     # the tests that read the site and the workflows
```

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
(kept 7 days); `docs-examples` builds the `lakana` wheel of the commit (`uv build --package lakana
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
## CI runners

CI jobs run on the runner named by the repository variable `CI_RUNNER`; when the variable is not
set, which is the normal state, they run on GitHub's `ubuntu-latest`. The switch exists for a
maintainer who wants to run CI on a runner of their own.

- **What moves:** every job of `ci.yml`, `security.yml`, `conventional.yml`, `mutation.yml` and
  `security-deep.yml`, and the `release-please` job of `release.yml`, which only opens and merges
  the release pull request.
- **What never moves:** the macOS shards `unit (macos, N/3)` (macOS runs only on GitHub's own runners) and every job
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
same way CI does, over the whole suite:

```bash
uv run pytest --cov --cov-report=json --cov-report=term
uv run python scripts/coverage_ratchet.py
```

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
  unmarked test over the threshold is run again on its own, serially and under coverage, and
  fails only if it takes `confirm_seconds` (2.5 s, in `scripts/time-budgets.toml` beside the
  threshold) or more there: a margin, because a test that sits at the 2 s threshold passed and
  failed on the same code. The in-shard threshold stays 2 s for flagging; the log names each one
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
step of the `unit (3.11, 3/3)` shard in `ci.yml` (about 3 min, not sharded itself): a first command asserts that `polars.__version__`
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
  `LAKANA_TEST_WHEELHOUSE` set; the job has `timeout-minutes: 30` and a budget of 15 minutes. Each test project commits
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
check `_check.py`: the Docker SDK reads only the root `.dockerignore`), creates a work volume, a server stand-in labelled as the stack's `server`
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
thus has its gateway refused from the next run on, as in production.

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
`test_connection_test_outcomes` (#308, M6 PR 5d) runs the launcher's `check` op against the same
source, with the routes and routing of `python_tasks.connection_plan` and the answer read as the
worker's loop reads it (`_relay_refusal`, then `_answer`): `ok` through the proxy with the trust
bundle; `tls` without it; `dns` for a name the proxy cannot resolve; `refused` for a closed port;
`not_allowed` for a token that does not name the host; `ok` for a PostgreSQL connection with
`tls: disable` and `tls` (not offered) without it, both through the relay; `refused` through the
relay to a closed port (the relay's line outranks the check's `ok`); `ok` for `tcp`; and
`root_unavailable` for a root the launcher does not mount. After each, nothing is labelled with
the check's id.

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
