# Projects and deploys

A Lakana **project** is a git repository of SQL and Python assets. Deploying a commit validates
it and records an immutable **project version** for one environment; runs execute it via
`lakana run`. Data quality checks are described in [Check your data](check-your-data.md);
jobs and their schedules are declared in `lakana.yaml` (see [Jobs](../reference/lakana-yaml.md#jobs)).

This page is the steps. The facts you look up are in the reference: the
[layout of a project](../reference/project-layout.md), the keys of an
[asset's front-matter](../reference/asset-front-matter.md), the keys of
[`lakana.yaml`](../reference/lakana-yaml.md), the rules of
[Python assets](../reference/python-assets.md) and the
[network settings of the worker](../reference/network-settings.md).

## Start a project

<!-- source: docs/examples/projects/run.sh#init -->
```bash
lakana init analytics          # lakana.yaml, a sample (three assets, one check), tests/,
                               # pyproject.toml, .github/workflows/lakana-deploy.yml,
                               # .gitlab-ci.yml
cd analytics && git init && git add -A && git commit -m "Initial project"
```

The sample is a source, `assets/bronze/readings.sql`, which generates its rows (nothing is
downloaded), two assets built from it (`assets/silver/station_temperatures.sql` and
`assets/gold/station_ranking.sql`) and one check, so a new project deploys, runs and can be queried
without writing a line; "Getting started" walks through it. It is written unless your default
profile (or `--url` or `LAKANA_URL`) points at an instance on another machine; `--sample` and
`--no-sample` decide it either way. The sample has no Python asset, because one needs the lock
described below; `lakana init --template python` writes the sample with one Python asset (see
below). `lakana init` ends with the next steps: commit, register, `lakana deploy --env dev`,
`lakana run --env dev` and a query of the result. If git cannot find a name and email from the
new directory (a fresh machine), `init` adds one line with the two `git config` commands, worded as a condition: use it
only if `git commit` says it does not know who you are. A per-directory identity (git's
`includeIf`) is not visible until the repository exists, so you may see the line and not need it.
With an identity found, `init` says nothing about git.

`lakana init` never overwrites a file: if one it would write exists, it writes nothing, names the
files and tells you to remove them (or the folder) or choose another directory. It also refuses a
folder inside the project that is a symbolic link, such as `assets` pointing elsewhere, because a
file written through it would land outside the project: remove the link or choose another
directory.

`lakana init --template python` writes the sample, one Python asset built from two of its tables
(`assets/gold/temperature_anomalies.py`), a `pyproject.toml` that depends on `lakana[assets]`, and
`uv.lock`: it runs `uv lock` itself. It says so first, because the lock needs network access to
the public package index and a few minutes; without `uv` on your `PATH`, or when the lock fails,
it stops with the cause and the next step and leaves nothing behind, also when you interrupt it.
Inside a uv workspace (a monorepo whose `pyproject.toml` has `[tool.uv.workspace]`) whose
`members` take in the new folder, `uv lock` would lock the monorepo instead, so `init` refuses
before it writes anything: it names the workspace's `pyproject.toml` and tells you to add the folder
to that workspace's `exclude`, or to run `lakana init` outside the workspace. With the folder
excluded, the project and the monorepo each lock on their own. Should uv still write no `uv.lock`
in the project, `init` fails and puts the monorepo's lock back as it was. `--sample` and
`--no-sample` are refused with it. The tutorial
[Build a table with Python](../tutorials/python-sample.md) walks through it.

`lakana init --template ingest-files` (or `ingest-sql`, `ingest-rest`) writes, instead of the
sample, one Python asset that reads a source system, a `pyproject.toml` that already depends on
`lakana[assets]`, and a `README.md` with the steps for its connection:
[Load data from a source](load-data-from-a-source.md).

The generated `pyproject.toml` does not depend on `lakana`: a SQL-only project needs no lock. The CI files pin
the version of the CLI that ran `lakana init` and install it from PyPI.
To add a Python asset, follow the numbered steps in the comments of that `pyproject.toml`:
depend on `lakana[assets]` (see "Python assets", step 4), run `uv lock`, and commit
`pyproject.toml` and `uv.lock`. A deploy that has Python assets but no usable lock says what is missing. A complete
example is in [Python assets](#python-assets). Local mode (`lakana server start --local`) runs Python assets when
`uv` is on your `PATH`; the Compose bundle's image ships it ([`deploy-with-compose.md`](deploy-with-compose.md), "Python
assets"). If a worker cannot run them, its startup log says why and what to set.
When the CLI is a development or local build (`.dev`/`+` versions), `lakana init` ends with a note:
that version is not on any index, so edit the pin in both CI files before you use CI. You can
ignore the note until then.

## Python assets

**SQL transforms, Python at the edges.** Write a `SELECT` as a SQL asset; use a Python asset for what
SQL cannot do. [Why, and how far a Python asset is trusted](../explanation/python-assets.md).
The rules of a Python asset, what a deploy checks, what a task may do and the operator's settings are
in the [Python assets reference](../reference/python-assets.md).

A Python asset can produce a table (the default) or an **artifact**: a file or a directory of
files, such as a trained model, that Lakana stores in numbered versions and that another asset
reads. This page covers table assets. For artifacts, see
[Train a scikit-learn model and score with it](models-with-scikit-learn.md) and the
[reference](../reference/artifacts.md).

### A first example

This walk-through starts from a new project and ends with a table built by a Python asset. It
needs a running instance (local mode is enough) and nothing else: the example creates its own
input tables.

**1. Create the project** (see [Start a project](#start-a-project)):

<!-- source: docs/examples/projects/run.sh#create -->
```bash
lakana init analytics --no-sample && cd analytics
```

**2. Add two small SQL assets** that hold the data. They are ordinary SQL assets with literal
rows. `assets/bronze/readings.sql`:

<!-- source: docs/examples/projects/project/assets/bronze/readings.sql -->
```sql
/* lakana
description: A few hand-written temperature readings.
*/
SELECT 'A1' AS station_id, TIMESTAMP '2026-01-01 00:00:00' AS ts, 1.0 AS temp_c
UNION ALL SELECT 'A1', TIMESTAMP '2026-01-05 00:00:00', 3.0
UNION ALL SELECT 'B2', TIMESTAMP '2026-01-06 00:00:00', 2.0
UNION ALL SELECT 'B2', TIMESTAMP '2026-01-07 00:00:00', 6.0
```

and `assets/bronze/stations.sql`:

<!-- source: docs/examples/projects/project/assets/bronze/stations.sql -->
```sql
/* lakana
description: The stations that report.
*/
SELECT 'A1' AS station_id, 'North' AS region
UNION ALL SELECT 'B2', 'South'
```

**3. Add the Python asset.** `assets/gold/station_scores.py` reads both tables and writes
`gold.station_scores`:

```python
import polars as pl
from lakana import asset


@asset(
    inputs={"readings": "bronze.readings", "stations": "bronze.stations"},
    owner="data-eng",
    tags=["weather"],
    retries=2,
    timeout="10m",
    columns={
        "station_id": "Station identifier.",
        "region": "The station's region.",
        "score": "Temperature spread over the week, in degrees C.",
    },
)
def station_scores(readings: pl.DataFrame, stations: pl.DataFrame) -> pl.DataFrame:
    """Temperature spread per station over the last week of readings."""
    recent = readings.filter(pl.col("ts") > pl.col("ts").max() - pl.duration(days=7))
    scored = recent.group_by("station_id").agg(score=pl.col("temp_c").std())
    return scored.join(stations, on="station_id")
```

**4. Add the dependencies.** The generated `pyproject.toml` carries the dependency on `lakana`
in a comment, pinned to the version of the CLI that wrote the file. Delete `dependencies = []`
and remove the `# ` in front of the `dependencies` line. `lakana` and every other package come
from the public package index: `uv lock` and the worker's first build of the project's
environment need a network connection to it (a worker with the docker runner reaches it through
the `egress` service, see [Run Python tasks in containers](run-python-tasks-in-containers.md#package-indexes-and-proxies);
one with the local runner uses `LAKANA_RUN__HTTPS_PROXY` if you set one, see
[Operator settings](../reference/python-assets.md#operator-settings)). With the line uncommented, the file reads:

```toml
[project]
name = "analytics"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["lakana[assets]==<version>"]

[dependency-groups]
dev = ["pytest>=8"]
```

The `<version>` that `lakana init` wrote is the version of the CLI that ran it. It must be the
version of the instance. To see which version the instance runs, run
`lakana --version`: it prints the CLI's version and the instance's (from `GET /api/v1/info`, which
needs a credential; in Python, `client.info().version`). It asks once and gives up after 3 s in all. If the
instance is down, too slow, answers like something else than Lakana, or no profile is configured, it says so and still exits 0. If the pin differs from
the instance's, the deploy refuses and names the version the instance runs (`uv.lock: has lakana X,
but this instance runs Y (…); update the dependency and run `uv lock``); change the pin to that
version.

A version of `lakana` that is not on PyPI, such as a build of a checkout, cannot be locked from
the public index. Build its wheel (`uv build --package lakana --wheel --out-dir wheelhouse`), put
it in a folder `wheelhouse` of the project and add a flat `uv` index for that folder as the only
source of `lakana`: a `[[tool.uv.index]]` table with `name = "lakana"`, `url = "wheelhouse"`,
`format = "flat"` and `explicit = true`, and `[tool.uv.sources]` with
`lakana = { index = "lakana" }`. Commit the folder with the lock.

`uv` is the Python package manager from Astral. Its lock file, `uv.lock`, pins every package and
its hash. You create it and commit it; Lakana never resolves versions for you:

<!-- source: docs/examples/train-and-predict/run.sh#lock -->
```bash
uv lock
```

**5. Commit, register and deploy.** A deploy reads a git commit, so commit everything first. An
admin registers the repository once ("Register the repository (admin)"). For a local path, that
is a repository under one of the instance's `LAKANA_REPO_ROOTS` (in local mode, your home
directory); for a hosted repository it is a pushed commit.

<!-- not tested: commits and deploys the page's Python asset, whose environment needs a lock and the public package index; the steps run in the example `train-and-predict` -->
```bash
git init && git add -A && git commit -m "Add station scores"
lakana repo add analytics --kind local --url "$PWD"      # once, admin
lakana deploy --env dev
```

**6. Run it and read the result:**

<!-- not tested: runs the page's Python asset, whose environment needs a lock and the public package index; the steps run in the example `train-and-predict` -->
```bash
lakana run --env dev
lakana sql "SELECT * FROM gold.station_scores" --env dev
```

The run builds `bronze.readings` and `bronze.stations` first, because the Python asset declares
them as inputs. The first run also builds the asset's environment, which takes a while.

### Two repositories, one artifact

When the producer of an artifact is in repository A and its consumer in repository B, the order
matters, because each deploy records what it sees:

1. A deploy of B **before** A has deployed fails with `table 'models.x' not found`.
2. After A's deploy and **before** A's first run, B deploys, but a run of B's consumer fails with
   `models.x has no version yet; run the asset that produces it`.
3. If the kind of the name changes later (A switches the asset between a table and an artifact),
   B's consumer fails with `models.x is a table, but this deploy recorded it as an artifact; ...` or
   `models.x is an artifact, but this deploy recorded it as a table; ...`. Redeploy B.

So deploy A first, run it once, then deploy B.

## Register the repository (admin)

<!-- not tested: registers a repository at a hosted git service with a token (`ghp_…`) that is the reader's own -->
```bash
lakana secret create github-token --env prod --value token=ghp_…   # per environment
lakana secret create github-token --env dev  --value token=ghp_…
lakana repo add analytics --kind https --url https://github.com/acme/analytics.git \
    --credential-secret github-token --default-ref main
```

Create the secrets first. An administrator who registers the repository binds each unbound secret of that
name to it, in every environment that has one; if they may not manage secrets in one of them, the
command is refused and binds nothing. Someone who may only register repositories
(`MANAGE_REPOSITORY`) can name a secret only if it is already bound to that repository; see
[Manage secrets](manage-secrets.md#bind-a-secret-to-its-use). A new URL for a repository with a
bound secret needs `MANAGE_SECRET` in that secret's environment too, because the URL is where the
secret goes.

- `https`: the secret's `token` is sent as the password with username `oauth2`, or with
  `--credential-username` (GitLab deploy tokens have their own).
- Behind a corporate proxy, or with an internal CA? See [Git network settings](../reference/network-settings.md#git-network-settings).
- `ssh`: the secret's `private_key` (OpenSSH format) and `--known-hosts-file` are required;
  unknown host keys are refused.
- `local`: an absolute path under `LAKANA_REPO_ROOTS` (a JSON list, e.g. `'["/srv/repos"]'`; empty
  refuses local repositories). Local mode allows your home directory; the Compose bundle mounts
  `./repos` at `/repos`. Only committed state is deployed. Treat the roots as **trusted content
  only**: a `.git` file, `objects/info/alternates` or `commondir` inside a repository can point
  outside the roots, so never put repositories you did not create under one.

`lakana repo remove NAME --yes` deletes a repository and its deploy history, but only while no
environment has a current deployment from it: undeploy it there first.

## Deploy, dry-run, roll back

<!-- not tested: the optional parts in brackets and `<id>` are the reader's to fill in; every command except `project-version show` runs without them in the example `projects` -->
```bash
lakana deploy --env dev [--ref main] [--dry-run]
lakana rollback --env dev --to-previous      # or --to <full sha>
lakana deploys list --env dev [--repo analytics] [--limit N]
lakana deployments --env dev
lakana project-version show <id> --env dev
lakana orphans --env dev                     # produced earlier, by no current deploy
```

A deploy prints the resolved SHA and a diff, and when the project has checks, a list of them under
`Checks:`, and one `warning:` line on stderr for each table that is kept after a failed check and
whose asset the deploy removes (see [Check your data](check-your-data.md); `--output json` has
them in `warnings`). The diff has: added, removed (now orphaned), adopted (the table
existed but no deployment produced it), materialization, SQL (formatting changes ignored) and
metadata changes. Jobs are listed apart from the tables, under `Jobs:`, as added, removed or
changed; a changed job names what changed: `selection` (the patterns or
`upstream`/`downstream`), `cron` (a schedule added or removed counts), `time zone` or `notify` (a
notification added, removed or pointed at another secret or format). A dry run
shows the schedule changes a deploy would make before you merge them; `--output json` has them as
`jobs_added`, `jobs_removed` and `jobs_changed` in `diff`. A reader who may not read an adopted
table sees it counted (`hidden_adopted`), not named; `project-version show` likewise drops the DAG edges into an asset whose SQL is withheld
and those from a table the reader may not read, which a `references:` target alone can make
(`hidden_edges`), the target of a generated `ref_` check, and the reference targets of an asset's model that the reader may not read
(`hidden_model_references`). Redeploying a SHA reuses its project version. Two deploys racing on one environment:
one wins, the other fails and can be retried. Two repositories racing to produce the same new
table are serialised by a per-environment advisory lock at record time (PostgreSQL): the second
fails with `table ... is produced by repository ...`. On SQLite (local mode, one worker, deploys
inline) nothing is serialised and the check is not race-proof; no race arises there. A deploy that loses a same-SHA race removes the code
tarball it uploaded when the winner stored a different object; a deploy that fails for any other
reason after uploading (a pointer conflict, the time limit) leaves its digest-named tarball under
`_artifacts/code/`. It is small, and the worker that holds the scheduler role removes it once it is
more than seven days old, no project version in any environment names it, and no deploy is queued or
running in an environment that uses the same storage root. Every `LAKANA_RUN__SWEEP_INTERVAL`
seconds (3600 by default; `0` turns the sweep off, and then nothing removes these tarballs)
it makes one pass that starts no new deletion after a minute (a storage call that hangs is not
interrupted); the worker's log says how many tarballs and bytes it removed, and how many storage
roots it skipped because a deploy was running. The sweep keeps a tarball that a project version in any
environment names by its location, or by its commit and digest, and it keeps every other file
under `_artifacts/code/`. If a tarball that a project version names is missing, the next deploy of that
commit to the same environment writes it again, provided the archive it builds has the recorded
digest; the worker's log says whether it was restored or why not. `--ref` takes a branch, a tag, `HEAD` (the server's default branch) or a full 40-character SHA,
never `refs/pull/*`, `refs/changes/*` or any other namespace; a SHA deploys only if a branch or tag of the
repository contains it. A rollback never contacts the git host.

A deploy, dry run or rollback that has to wait for a worker says why. After 3 seconds in the queue,
`lakana deploy` and `lakana rollback` print a line on standard error, `Waiting: <the sentence>`, and
print another only when the reason changes (nothing is printed with `--output json`). The reasons
are `no_worker`, `no_worker_of_kind` (no worker takes pipeline work), `workers_busy` and
`starting`; [Why is my query or task waiting?](why-is-it-waiting.md#a-queued-deploy) says what to
do for each. In the SDK, `client.deploys.wait(env, id, on_update=f)` calls `f` with every polled
deploy, and `deploy.waiting_for` holds the reason.

A deploy, dry run or rollback whose worker is lost (killed, stopped for memory, or cut off from
the database, so that its heartbeat is a lease old) ends `failed` within about a minute with the defaults,
with the message `The worker running this deploy was lost, so the deploy was ended. Nothing was
activated. Deploy again.` Run the command again. Nothing was published by the lost deploy: the
project version, the deployment pointer, the status and the audit event are written in one step that
only the worker the run belongs to can take, so a deploy that was merely frozen and finishes
after it was ended changes nothing and the run stays `failed`. A worker whose heartbeat is stale
does not stop a deploy that is still running by itself: the deploy ends when the scheduler ends
it, or when it tries to finish. A worker that is alive but could not write the final status of a
deploy (the database was away) ends that deploy itself the same way, right after the next query,
deploy or task it finishes and at least every few seconds while it is idle; nothing was published
then either. The code archive that a lost deploy uploaded stays under
`_artifacts/code/` like the one of any other failed deploy, until the sweep described above removes it.

A deploy fetches the commit into a `lakana-deploy-…` directory under the worker's temp directory
and removes it when it ends. If the worker is killed, the directory stays; a worker removes such
directories that no deploy is using and that are more than 24 hours old, when it starts and then
every `LAKANA_RUN__SWEEP_INTERVAL` (`0` turns this off).

An orphan is a table that still exists, that an earlier project version in the environment produced,
and that no repository's current deployment there produces. A table moved from one repository to
another is therefore not an orphan once the new owner has deployed it.

## Run

<!-- not tested: the optional parts in brackets and `RUN_ID` are the reader's to fill in; `run` with its selections and `runs list` run in the example `projects`, while `runs show`, `logs`, `cancel` and `resume` need the id of a run -->
```bash
lakana run --env dev                                   # the whole DAG, waits, exit 0/1
lakana run --env dev --select 'gold.*' --upstream      # gold and everything it reads
lakana run --env dev --select silver.clean --downstream
lakana run --env dev --no-wait                         # print the run id and return
lakana runs list --env dev [--repo analytics] [--status failed] [--since 2026-10-18] [--limit N]
lakana runs show RUN_ID --env dev
lakana runs logs RUN_ID --env dev [--task gold.daily] [--follow]
lakana runs cancel RUN_ID --env dev
lakana runs resume RUN_ID --env dev                    # failed + upstream_failed + cancelled, and their downstream
```

A run executes the deployed project version, never your working tree: one task per asset in
dependency order, in parallel where the DAG allows. After a task has written its table, the
checks of that table run, and the task ends `failed` when a blocking check fails (see
[Check your data](check-your-data.md)).

`lakana run` prints the run id first (stderr), then one plain line per task status change, then a
summary (every task; at most 50 error texts, then `+N more failed tasks` — `--output json` has
them all). `lakana runs list` returns the newest 100 runs, or `--limit N` (1–200); `--status`, `--trigger`,
`--job`, `--since` and `--repo` narrow it and a long list names the command for the next page (see
[Find a run, a query or a deploy](find-a-run.md)). Exit 0 on
success; 1 when the run fails, is cancelled or `--timeout` elapses (the run keeps going; the message names `lakana runs show RUN_ID --env ENV`); 7 when a run of the repository
is already active. `--no-wait` prints only the id.

A task that has not started says why: the task table of `lakana runs show` has a `waiting_for`
column, and the line of `lakana run` for a task ends with the same sentence (for example
`Ready, and all 2 workers are busy. Queued queries and deploys are served first.`). A task that
waits to try again prints the time of the next attempt instead of `retrying after attempt n/max`.
[Why is my query or task waiting?](why-is-it-waiting.md) lists every sentence and what to do about it.

A task that fails has a stable code and a next step. `lakana runs show` prints both under the
error, and `lakana run` prints the next step when a task fails: `failed: timeout. Next: Make the
task faster, or raise its timeout in the project and deploy again.`
[Why did it fail?](../reference/why-did-it-fail.md) lists every code and what to do about it.

### Selection patterns

`--select` (repeatable) takes case-sensitive shell globs (`*`, `?`, `[abc]`) over dotted table
names. `*` crosses namespace dots: `gold.*` selects everything under `gold`, including nested
namespaces such as `gold.finance.daily`. A bare `gold` matches nothing; use `gold.*`. A pattern
that matches no asset is an error. `--upstream` adds every ancestor of the matched assets,
`--downstream` every descendant. Upstream assets outside the selection are taken as they are; if
one has never been built, its downstream task fails with "upstream table '<name>' has not been
built". A selected asset that reads another selected asset only through a view outside the
selection waits for that asset, and becomes `upstream_failed` if it fails.

At most one run per repository and environment is active; a second `lakana run` exits 7 and names
the active run. A failed task makes every task downstream of it `upstream_failed`;
`lakana runs resume` reruns only those, as long as the deployment still points at the same
project version (after a redeploy, start a fresh run). Each task commits atomically, so a failed run
never leaves a half-written table, but readers can see a mix of old and new tables while a run
is in progress. A SQL task whose write committed just before its worker was lost (or crashed right
after its commit) is **not** run again: when its lease runs out the platform finds the committed
write and ends the task `succeeded`, with the rows the write stored and a message that says the
worker was lost afterwards (see [Failure, retry and cancel](../reference/python-assets.md#failure-retry-and-cancel)). The same holds when the worker is
alive but could not record the outcome after the commit, also during a drain or a cancel. The exception
is a task whose table has [checks](check-your-data.md): the platform does not end it `succeeded`
before its checks have run. It is taken again, builds nothing, and only runs its checks. A task whose write did not commit
is tried again under its policy, and an attempt that has already been superseded never commits.
`replace` rebuilds the same table, `append` re-reads its watermark from the table and `merge`
matches the rows it wrote before on their key, so a rerun after a failure of another kind is safe
too. A Python task is still run at least once, not exactly once: if its worker is lost after the
function wrote a table, the task is run again, so write with `replace`, `merge` or an `append`
with a watermark (see [Write modes](../reference/asset-front-matter.md#write-modes)). The
platform does not filter what a Python function returns, so a Python `append` must return only
the rows past `ctx.watermark()` (with `ctx.watermark(default=...)` for the first run); rows it
returns again that are not past the watermark are added again (see
[The asset's context](../reference/python-assets.md#the-assets-context)). (A Python task with checks that is lost after its
last attempt's write has its table set back first; one without checks keeps what it wrote.)
Starting, cancelling and resuming need `RUN`; reading needs `READ_ENVIRONMENT` (a `RUN` holder
can read the runs it starts). `GET .../runs` takes `limit` (1 to 200).

A Python asset runs as a subprocess of the worker that claimed it. While that process runs, the
same worker keeps executing queries (and nothing else: no second task, no deploy), so the queries
that load the asset's inputs are served even when there is only one worker. The task's log
(`lakana runs logs`) holds, per attempt, the output of the environment build and of your code
(`print` and tracebacks included), up to 1 MiB, with the task's token redacted. What fails, what is
retried and what the messages mean: [Python assets](../reference/python-assets.md), [Failure, retry and cancel](../reference/python-assets.md#failure-retry-and-cancel).

`lakana runs logs RUN_ID --follow` asks the server only for what is new: it remembers the byte it
reached per task and fetches from there, so a long log travels once. It ends when the run has
finished or, with `--task`, when that task has finished and its log is read to the end. Without
`--follow` the whole log is printed, as before.

The same works over REST and in the SDK (`client.runs.log(env, run_id, asset, offset=N)`):
`GET /api/v1/environments/{env}/runs/{run_id}/tasks/{asset}/log?offset=N`. Without `offset` the answer
is the whole log. With it, `text` starts at byte `N` of the log and the answer adds:

| Field | Meaning |
|---|---|
| `next_offset` | The byte where `text` ends. Send it as the next `offset`. Always present. |
| `more` | `true` when stored bytes remain after `next_offset`: one answer holds at most 1 MiB, so ask again at once. |
| `complete` | `true` when the task has finished and `text` reaches the end of its log: stop asking. The worker stores an attempt's log block before it marks the attempt finished, so a finished task never has log text still to come. A task the platform ends itself (its worker was lost, or the memory limit stopped it) gets no more log, so `complete` stays true there too. If the block could not be stored, the worker logs a warning and still ends the attempt. |
| `reset` | `true` when `offset` was beyond the end of the log (it is shorter than before, for example removed). `text` is empty; continue from `next_offset`. |

Offsets count the bytes of the text you receive, which is stored already redacted, so a cut between
two answers never reveals a secret. An answer never ends inside a multi-byte character: it stops
before it, and `next_offset` says where to continue. An `offset` that falls inside a character
starts at that character's first byte. An `offset` that is negative or not a whole number is a 422;
one beyond the end is not an error. Rights are those of the whole-log request.

`lakana scheduler status` (admin) shows which worker holds the scheduler role and the queue.
Set `LAKANA_RUN__*` and `LAKANA_QUERY_LIMITS__*` where both the server and the workers read them (the
Compose bundle's shared `x-lakana-env` block).

Two settings limit memory. `LAKANA_QUERY_LIMITS__MEMORY_LIMIT` (about 1 GB) is DuckDB's target for
one query; some operations go beyond it. `LAKANA_WORKER__MEMORY_LIMIT` (4 GB, `off` to disable,
at least 512 MB) is the stop for the whole worker: it covers the worker, its queries and the Python tasks
it started. On Linux, a worker over it first ends the largest running query or Python task: a
query fails with the cause `out_of_memory`, a task's attempt fails and is retried (see [Failure, retry and cancel](../reference/python-assets.md#failure-retry-and-cancel)); the worker and everything else go on. Only if the worker is still
over the limit after that, or nothing it can end was running, does it stop. It is then restarted, and the
query or task it was running is marked failed with the cause `out_of_memory` (see [Failure, retry and cancel](../reference/python-assets.md#failure-retry-and-cancel) for what happens to a task). On macOS the worker always stops, without ending a query first.
A sort or aggregation over DuckDB's limit fails as out of memory too (`the query ran out of memory and cannot spill to disk`, cause `out_of_memory`): DuckDB may spill to disk only with local `file://` storage; every other deployment gives it no spill space. Keep the first well below the second. Sizes in the second are binary (`GB` is 1024 MB); in
the first, `GB` is decimal. Linux measures the worker and its tasks, macOS only the worker process,
and Windows nothing. In the Compose bundle, set `LAKANA_WORKER_MEMORY` in `.env` instead
([`deploy-with-compose.md`](deploy-with-compose.md#memory-limits), "Memory limits"); local mode
reads the environment variable.

**Polars inside a task.** Polars 2 can write intermediate results to disk instead of holding them in
memory ("spilling"). Left alone, it decides from the memory of the machine, and into a directory
under `/var/tmp` that every task shares and nothing removes. Inside a task the machine's figure
would be wrong: Polars reads the machine's total memory, not the worker's limit or the
container's, so on a large host the stop of `LAKANA_WORKER__MEMORY_LIMIT` could come first. The
worker therefore asks Polars for these things when it starts a task:

- `POLARS_OOC_SPILL_DIR` is a directory of the task's own in its private temporary directory,
  deleted with the attempt. It is set always, also with the limit `off`.
- With a memory limit, `POLARS_OVERRIDE_TOTAL_MEMORY_MB` is that limit, so Polars takes it for the
  memory of the machine (its spilling starts from a fraction of that figure; Lakana does not
  change the fraction), and `POLARS_OOC_DISK_BUDGET_MB` is the same number of megabytes, which
  asks Polars to keep what it writes to disk within it.
- With the limit `off`, the two sizes are not set and Polars keeps its own defaults for them.

These are requests to Polars, which Lakana passes and does not enforce: what stops a task is
the worker's memory limit. The task's own Python objects and Arrow copies count toward the limit
but not toward Polars' budget, so a task that holds a large input as well can still reach the
limit: keep inputs narrow. Polars 1 does not spill in this way and ignores the settings.

| Setting | Default | Meaning |
|---|---|---|
| `LAKANA_RUN__MAX_RETRIES` | 5 | Most retries a project may ask for per asset; a deploy asking for more fails validation. |
| `LAKANA_RUN__SCHEDULER_INTERVAL_SECONDS` | 1 | How often the worker holding the scheduler role ticks (readiness, backoff, lost leases, cancellation, finalization). It also paces the sweep that ends the queries and deploys of lost workers, so keep it well below half of `LAKANA_RUN__LEASE_SECONDS`. |
| `LAKANA_RUN__LEASE_SECONDS` | 60 | How long a claimed task's lease lasts without renewal, and how old a worker's heartbeat may be before the worker counts as lost. A worker that stops renewing is declared lost and its task retried, unless its write had committed (then the task is `succeeded`); the query runs it held are ended (`FAILED` with the cause `worker_lost`, or `SUCCEEDED` for a write that had committed). The server and every worker must read the same value. Lease times are the database's clock, never a worker's. |
| `LAKANA_RUN__RENEW_SECONDS` | 20 | How often a worker renews its lease and writes its heartbeat; must be below the lease length. |
| `LAKANA_RUN__DRAIN_GRACE_SECONDS` | 20 | After SIGTERM a worker claims nothing new and lets its running task finish for this long, then releases it to another worker (not counted as an attempt). A Python asset's process is asked to stop at that point and gets `LAKANA_RUN__KILL_GRACE` (10 s) to exit. Keep the container's stop grace period above the two together plus a few seconds. |

## Undeploy

<!-- source: docs/examples/projects/run.sh#undeploy -->
```bash
lakana undeploy analytics --env dev --yes
```

Removes the deployment pointer and the identity its runs use (needs `DEPLOY`); refused while a run
of the repository is queued or running there. Data is untouched: the repository's tables become
orphans (`lakana orphans`), and its project versions stay as history.

An environment cannot be deleted while a repository is deployed in it: `lakana env delete ENV`
answers 409, naming the deployed repositories. Undeploy each one first, then delete.

## CI deploys to protected environments

"Only CI deploys to prod" is a grant: give `DEPLOY` on `prod` to a CI service principal and to no
one else. The `ADMIN` role holds `DEPLOY`, like every other action but `READ_QUERY_TEXT`, so the
bootstrap admin and any principal with `ADMIN` on the instance or on `prod` can still deploy to prod
from a laptop; the guarantee holds only while no human holds `ADMIN` there. Use **two** principals,
because a pull request runs the workflow file from its own branch: anyone who can push a branch can
edit it, so whatever token a PR job sees must be worthless on prod.

The CI files `lakana init` writes store no Lakana token at all: each job exchanges its OIDC ID
token for a CI token that may queue one deploy of the job's own commit, or one dry run, under a
trust rule an administrator creates once. The set-up is in [Deploy from GitHub Actions without a
stored secret](deploy-from-github-actions.md) and [Deploy from GitLab CI without a stored
secret](deploy-from-gitlab-ci.md). The rest of this section holds for both kinds of CI file.

### With a stored token

`lakana init --ci-auth pat` writes CI files that read a stored Lakana token instead, and CI that
already uses one keeps working. Create the two principals and their tokens:

<!-- not tested: needs a protected `prod` environment and a CI system, which the one-instance examples do not set up -->
```bash
lakana principal create ci-dev  --type service-account
lakana principal create ci-prod --type service-account
lakana token issue ci-dev github-dev                # CI secret LAKANA_TOKEN_DEV
lakana token issue ci-prod github-prod              # CI secret LAKANA_TOKEN (GitHub) or
                                                    # LAKANA_TOKEN_PROD (GitLab), prod only
lakana grant create --principal ci-dev  --action DEPLOY --kind ENVIRONMENT --env dev
lakana grant create --principal ci-prod --action DEPLOY --kind ENVIRONMENT --env prod
lakana grant create --principal ci-dev  --role VIEWER --kind ENVIRONMENT --env dev    # read inputs
lakana grant create --principal ci-prod --role VIEWER --kind ENVIRONMENT --env prod
```

The deploying principal must be able to read every external input, hence the VIEWER grants.
`DEPLOY` is per environment, not per repository: a principal holding it on `prod` can deploy any
registered repository there.

These CI files dry-run pull/merge requests against `dev` with `LAKANA_TOKEN_DEV` and deploy
`main` to `prod` with the prod token; both read `LAKANA_URL` from CI secrets.

- **GitHub:** put `LAKANA_URL` and `LAKANA_TOKEN_DEV` in repository secrets. Only for a mirror, put
  `LAKANA_INDEX_URL` in repository variables (if the URL carries credentials, make it a secret and
  change `vars.` to `secrets.` in the workflow). Create the `prod` environment, restrict it to the `main` branch (and
  optionally required reviewers), and store `LAKANA_TOKEN` as an *environment* secret there. The
  workflow has `contents: read` only and serialises deploys per target. Fork pull requests get no
  secrets, so their dry-run is skipped.
- **GitLab:** protect `main` and no other branch, then add `LAKANA_URL`,
  `LAKANA_TOKEN_DEV` (masked) and `LAKANA_TOKEN_PROD` as a variable that is both *masked* and
  *protected*; add `LAKANA_INDEX_URL` only for a mirror. GitLab exposes a protected variable to every pipeline on a protected branch or tag,
  including a merge request pipeline whose source branch is protected, so keep the protected set to
  `main`. Each job maps its own variable to `LAKANA_TOKEN`; never define a project variable named
  `LAKANA_TOKEN`, because project variables override a job's `variables:`. The prod job uses a
  `resource_group` so deploys do not race.

### What both kinds of CI file do

Every `lakana deploy` in them passes `--timeout 1800`, so a deploy that has not ended after 30
minutes fails the job (exit 1) instead of holding it until the runner's own limit.

The job that deploys to prod installs `lakana` from one index, not from whichever index pip
happens to know. Both CI files install with
`pip install --index-url "${LAKANA_INDEX_URL:-https://pypi.org/simple}" "lakana==<version>"`:
from PyPI when the variable `LAKANA_INDEX_URL` is unset or empty. To use a mirror, or a build that
is not published, set `LAKANA_INDEX_URL` to an index you control that hosts that version of
`lakana` and mirrors its dependencies. Never switch to `--extra-index-url` next to PyPI: pip
takes the name from whichever index offers it.

The GitHub workflow pins each action (`actions/checkout`, `actions/setup-python`) to a full commit
SHA with the release in a trailing `# vX.Y.Z` comment, since a mutable tag in the job that deploys
to prod is a supply-chain hole; bump them by hand or with Dependabot. The pinned actions run on node24 and need runner
2.327.1 or newer; older self-hosted and GitHub Enterprise Server runners fail on them.

A DEPLOY-only principal can poll its own deploy run, so `lakana deploy` prints
`Queued deploy <id>` and then the result; it cannot read manifests, deploy history or orphans
unless it also holds read access to the environment. A CI token (the files without a stored
token) may only queue its one run and follow it: see [CI trust rules and CI
tokens](../reference/ci-trust-rules.md).

## Run principals

In a protected environment a run writes as the service account bound to the deployment — never as
whoever triggered it:

<!-- not tested: needs a service account `etl-prod` and a protected `prod` environment, which the one-instance examples do not set up -->
```bash
lakana deployments set-run-as analytics --env prod --principal etl-prod
lakana deployments --env prod            # shows run_as per repository
lakana deployments set-run-as analytics --env prod --clear
```

Binding needs `MANAGE_ENVIRONMENT`; the principal must be an active service account (an unknown
name and a user both answer 404 "not found or not a service account"). Clearing a deployment
that has no binding changes nothing and is not audited. The binding
belongs to the deployment and survives deploys and rollbacks; a running run keeps the principal it
started with. Grant the run principal what its assets need — `READ_TABLE` on its inputs,
`CREATE_NAMESPACE`, `CREATE_TABLE` and `WRITE_TABLE` where it writes — and nothing else. In an
unprotected environment a run writes as the bound principal if one is set, otherwise as the
caller.

## Protected environments: lockdown and break-glass

In a protected environment (`lakana env protect prod`) only runs of the deployed project version write.
Every other write — `lakana sql … --into`, `lakana table create|append|replace|delete`,
`lakana ns create|delete`, an SDK write, a PyIceberg commit, or one from another engine, through the Iceberg REST
catalog — is refused with 403 "Environment 'prod' is protected: only runs of the deployed
project version may write. See `lakana run`.", even when the caller's grants allow the write (a caller
without them gets the ordinary 403 or 404 first). Reads, grants, policies,
secrets, repositories, deployments and table maintenance are unaffected.

A protected environment runs Python assets only on workers that run tasks in containers
(`LAKANA_RUN__RUNNER=docker`, the Compose bundle's default). A worker with the local runner fails
such a task at once with "environment 'prod' is protected, and this worker runs Python tasks
without isolation (LAKANA_RUN__RUNNER=local). …" (failure code `no_runtime`); give every worker that
takes pipeline work the docker runner before you protect an environment with Python assets.
This is also the security condition, not only a matter of where tasks run: a task on the local
runner, of any environment, runs as the worker's own user and can read the worker's environment,
which holds the database URL (`LAKANA_DATABASE__URL`) and the master key
(`LAKANA_SECRETS__MASTER_KEY`). With those it could write to a protected environment and read every
secret directly, and protection would mean nothing. Local
mode runs tasks without containers, so it cannot run the Python assets of a protected environment.
Protection is read when a task's attempt is prepared: an attempt already under way on a local-runner
worker runs to its end. SQL assets are not affected. See
[Run Python tasks in containers](run-python-tasks-in-containers.md).

For an incident, a holder of `MANAGE_ENVIRONMENT` can make exactly one write anyway:

<!-- not tested: writes to a table of a protected `prod` environment, which the one-instance examples do not set up -->
```bash
lakana table append bronze.events fix.csv --env prod --break-glass --reason "INC-42: replay lost rows"
```

The reason (10–500 characters on one line, no control, invisible formatting or line-separator characters) is recorded in the audit log as `BREAK_GLASS_WRITE` with the
principal, operation and table; grants and policies still apply. The Iceberg REST catalog has no
break-glass. The next run of the deployed code may overwrite the fix: real fixes go through a
deploy.
