# lakana

> **Pre-beta.** Lakana is published as a 0.x pre-beta: the API and the data layout may change
> between releases, and it is not ready for production data yet. See the
> [project README](https://github.com/lakana-labs/lakana#readme).

A typed Python client for the Lakana platform API. `lakana` never imports `lakana-core`,
`lakana-server`, or `lakana-worker` — it only speaks HTTP to a running Lakana server, over the
same REST surface any other client would use.

## Install

```bash
pip install lakana
```

The only runtime dependencies are `httpx` and `pyarrow`.

## Credentials

Every request authenticates with a personal access token (PAT) or a task token, sent as a
bearer credential. An administrator issues a PAT for your principal:

```python
admin_client.tokens.issue("alice", "alice-laptop")  # -> starts with "lakana_pat_"
```

Pass the token to `Client` however is convenient for your environment — see "Configuration"
below for the full resolution order.

## Quickstart

```python
import lakana
import pyarrow as pa

client = lakana.Client(url="https://lakana.example.com", token="lakana_pat_...")

client.environments.create(
    "dev",
    storage={"scheme": "s3", "root": "s3://my-bucket/dev", "options": {}},
)

schema = pa.schema([pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())])
client.tables.create("dev", "bronze.sales", schema=schema)

data = pa.table({"id": [1, 2, 3], "region": ["eu", "us", "eu"]}, schema=schema)
client.tables.append("dev", "bronze.sales", data)

result = client.sql(
    "SELECT region, count(*) AS n FROM bronze.sales GROUP BY region", environment="dev"
)
print(result)
```

`client.sql(...)` submits a query, waits for it to finish, and returns the result as a
`pyarrow.Table` in one call; `client.queries.submit(...)` returns a `QueryRun` handle for
polling or waiting explicitly when you need more control. `run.cancel()` (or
`client.queries.cancel(query_id, environment)`) cancels a queued query and asks the worker to stop a
running one; it returns the `QueryStatus` after the request. A cancelled query has the status
`CANCELLED`, and `run.wait()` then raises `QueryFailed` with `cause == "cancelled"`. `client.sql`
does not cancel when your code is interrupted.

### DataFrames: polars by default

Arrow is the contract underneath, and **polars** is Lakana's default DataFrame tool. The SDK
itself only depends on pyarrow (`pip install polars` separately); results convert
zero-copy, and `create`/`append` accept polars schemas and DataFrames directly:

```python
import polars as pl

frame = pl.DataFrame({"id": [1, 2, 3], "region": ["eu", "us", "eu"]})
client.tables.create("dev", "bronze.sales_pl", schema=frame.schema)
client.tables.append("dev", "bronze.sales_pl", frame)

df = pl.from_arrow(client.sql("SELECT * FROM bronze.sales_pl", environment="dev"))
```

polars exports text and bytes as Arrow `string_view`/`binary_view`; the SDK sends them as
plain `string`/`binary`, since the append endpoint refuses view types. pandas works the
same way (`client.tables.append(..., pandas_df)`, `result.to_pandas()`).

Use `client` as a context manager (`with lakana.Client(...) as client:`) or call
`client.close()` when done, to release the underlying HTTP connection pool.

### Writing results into tables, views, lineage and maintenance

A query's result can go straight into a table instead of back to you. The write runs in a
worker, under your own grants, and commits once: readers see the old table or the new one,
never a mix.

```python
client.sql("SELECT * FROM bronze.sales", environment="dev", into="silver.sales", mode="create")
client.sql(
    "SELECT region, count(*) AS n FROM silver.sales GROUP BY region",
    environment="dev",
    into="gold.daily",
    mode="replace",  # a full refresh, in one snapshot
)
status = client.sql(
    "SELECT * FROM bronze.events",
    environment="dev",
    into="silver.events",
    mode="append",
    incremental_key="ts",  # appends only rows past the table's max(ts)
)
print(status.rows_written, status.watermark)
```

- `mode` is `create`, `replace`, `append` or `view`. With `into`, `client.sql` returns the
  finished run's status (`rows_written`, `snapshot_id`, `watermark`, `schema_diff`), not a
  table.
- A result whose schema doesn't match the table fails before anything is written, with a
  diff of added, dropped, renamed-looking and retyped columns. `evolve_schema=True` lets the
  table adopt the result's schema instead. Extra nullable columns are always added.
- Row filters and column masks on what you read apply to what you write: masked values land
  masked.
- Late rows (`ts` at or below the watermark) are skipped by an incremental append, by
  design; merge and upsert come after 1.0.
- An incremental append needs READ on the target, and is refused while a row filter or column
  mask on the target applies to you.
- `mode="view"` stores the SELECT as a view without running it. Views use invoker's rights:
  reading one needs READ on the view and on every table beneath it, and those tables' row
  filters and masks still apply. A view's SQL and references are shown by `tables.get` only if
  you can READ every table it references; otherwise you get `hidden_references` (a count). A
  query expands at most 100 view references. Views are not visible over the Iceberg REST
  catalog, and can't be dropped through it.

Arrow data has the same two write shapes, and can declare where it came from:

```python
client.tables.replace("dev", "gold.predictions", frame, sources=["gold.features"])
client.tables.append("dev", "gold.predictions", frame, sources=["gold.features"])
```

A read-modify-write job can make either write conditional on the table not having changed since
it read it. Pass the `snapshot_id` you read (`client.tables.get(...).snapshot_id`), or `"none"`
for "no snapshot yet":

```python
current = client.tables.get("dev", "gold.predictions").snapshot_id
client.tables.replace("dev", "gold.predictions", frame, expected_snapshot_id=current)
```

If another writer committed in between, the write raises `Conflict` and nothing is retried; read
again and redo it. On `replace`, `"none"` also creates a table that does not exist yet from the
upload's schema (you need `WRITE_TABLE` and `CREATE_TABLE`). On `append`, a missing table is
still `NotFound`.

Every write records table-level lineage, which you can walk:

```python
graph = client.tables.lineage("dev", "gold.daily", direction="upstream", depth=3)
```

Tables you can't read are left out of the graph and counted in `graph.hidden_nodes`; the walk
never goes through them. A graph has at most 500 nodes; `graph.truncated` is set when a table
you can read was left out past that cap.

Maintenance expires old snapshots (older than `keep_days`, beyond the newest `keep_last`,
never the current one or a branch or tag) and deletes files nothing references any more:

```python
run = client.tables.maintain("dev", "gold.daily", keep_days=7, keep_last=5, dry_run=True)
print(run.wait().maintenance["report"])
```

- Files are deleted only once they are at least `older_than_hours` old (default 72, minimum 1,
  maximum 87600). Set it longer than your longest external write to the table.
- Expiry commits only against the metadata it selected from: if the table changed meanwhile,
  nothing is deleted and the report sets `conflict: True` (note "table changed during
  maintenance, retry"); the CLI exits 1. Check `report["conflict"]`, not the note's wording.
- If the table changes after the expiry but before orphans are deleted, orphan removal is
  skipped and the report's note says so.
- If any file the table references lies outside its location, orphan removal is skipped and
  the report says why.
- It needs the `MAINTAIN_TABLE` action, which only the built-in admin role carries.

## Configuration

`Client(url=None, token=None, *, profile=None, timeout=30.0, verify=True, http_client=None)`
resolves its URL and token from the first source below that names either one — sources are
never merged, so a source that sets one of `url`/`token` must set both:

1. **Explicit constructor arguments** — `url` and `token`.
2. **Environment variables** — `LAKANA_URL` and `LAKANA_TOKEN`. `LAKANA_PROFILE` selects a named
   profile from the profile file if you set it alongside these (otherwise it has no effect
   at this step).
3. **The profile file** — `$XDG_CONFIG_HOME/lakana/config.toml`, or
   `~/.config/lakana/config.toml` when `XDG_CONFIG_HOME` is unset:

   ```toml
   default_profile = "dev"

   [profiles.dev]
   url = "https://lakana.example.com"
   token = "lakana_pat_..."
   ```

   `profile=` (or `LAKANA_PROFILE`) selects a profile by name, falling back to the file's own
   `default_profile`. The file must be readable only by its owner (mode `0600` or
   stricter) — the SDK refuses it otherwise, telling you the `chmod` to run. It is read
   through a single `O_NOFOLLOW` open, so a symlinked profile file is refused outright as a
   TOCTOU precaution; a symlinked *parent directory* is a known, unclosed limitation of that
   check (see <https://lakana-labs.github.io/lakana/explanation/compose-known-gaps/#a-symlinked-parent-directory-of-the-profile-file>).

If none of these resolves a URL and token, `Client(...)` raises `lakana.ConfigError`.

In a GitHub Actions or GitLab CI job, `Client.from_ci_oidc(url, *, environment, repository,
provider=None, id_token=None)` needs no stored token: it exchanges the job's OIDC ID token for a
CI token that may queue one deploy of the job's commit, or one dry run. `provider="github"` asks
the runner for a new ID token, `"gitlab"` reads `LAKANA_OIDC_TOKEN`, and `None` reads
`LAKANA_OIDC`; build one client per submission. See
<https://lakana-labs.github.io/lakana/how-to/deploy-from-github-actions/> and
<https://lakana-labs.github.io/lakana/how-to/deploy-from-gitlab-ci/>.

`http_client` accepts any `httpx.Client`-compatible object in place of one the SDK builds
itself — useful for tests (see `lakana-server`'s own SDK end-to-end tests, which pass a
Starlette `TestClient` this way).

A token is never written into a log line, an exception message, or a `repr()` — `Client`
and its resolved config both mask everything but the token's key-id prefix, e.g.
`Client(url='...', token='lakana_pat_<key>_***')`.

## What's covered

`client.<resource>` exposes every `/api/v1` platform route: `environments`, `namespaces`,
`tables` (including `append`, `replace`, `lineage` and `maintain`), `queries`,
`principals`, `groups`, `tokens`, `task_tokens`,
`grants`, `row_filters`, `column_masks`, `secrets`, `repositories`, `deploys`,
`deployments`, `project_versions`, `orphans`, `artifacts`, `runs`, `jobs`, `checks`, and `audit_events`. A contract test
(`packages/lakana-server/tests/sdk/test_sdk_contract.py`) checks in both directions that this
SDK never calls a route the server doesn't serve, and that every platform route has an SDK
method — so this list can't silently drift from what the server actually exposes.

The Iceberg REST Catalog routes (`/v1/...`) are a separate protocol for external Iceberg
clients (e.g. `pyiceberg`) and are out of scope for this SDK.

Errors raise typed exceptions from `lakana.errors` (`AuthenticationError`,
`PermissionDenied`, `NotFound`, `Conflict`, `PayloadTooLarge`, `ValidationError`,
`ServerError`, `LakanaConnectionError`, `QueryFailed`, `DeployFailed`, `ConfigError`), all subclasses of
`lakana.LakanaError`.

`client.info().version` is the platform version the instance runs (the same value `lakana --version` shows).

## Command line

`pip install lakana` also installs the `lakana` command.

```bash
lakana login                       # prompts for URL and token; saves ~/.config/lakana/config.toml (0600)
lakana whoami
lakana --version                     # the CLI's version and the instance's (GET /api/v1/info; one request, no retry, gives up after 3 s in all)
lakana env create dev --storage-root file:///srv/lake
lakana env delete dev --yes        # 409 while a repository is deployed there: `lakana undeploy REPO --env dev` first
lakana table create bronze.sales --column id:long --column 'region:string?'  # quote: shells glob ?
lakana table append bronze.sales sales.csv      # Parquet, CSV or Arrow; a CSV is parsed with the table's types
lakana load customers.csv customers --env dev  # once, into bronze.customers: shows schema and rows, asks; a first load creates the table
lakana sql "SELECT region, count(*) FROM bronze.sales GROUP BY region"
lakana sql - --out report.parquet < report.sql
lakana sql "SELECT * FROM bronze.sales" --into silver.sales --mode create
lakana sql "SELECT region, count(*) AS n FROM silver.sales GROUP BY region" --into gold.daily --mode replace
lakana sql "SELECT * FROM bronze.events" --into silver.events --mode append --incremental-key ts
lakana query submit "SELECT ..." | lakana query status ID | lakana query wait ID | lakana query result ID
lakana query cancel ID                              # queued: cancelled at once; running: asked to stop
lakana query list --env dev [--all] [--limit N]     # queued and running; --all adds the last 24 h; no SQL
lakana table replace gold.predictions predictions.parquet --source gold.features
lakana table lineage gold.daily --depth 5          # --downstream for what it feeds; --output json
lakana table maintain gold.daily --dry-run          # --keep-days 7 --keep-last 5 --older-than-hours 72
lakana search orders --kind column --limit 10 [--offset N]  # tables, columns, descriptions, runs, people you may see; SDK: client.search(env, q)
```

- Ctrl-C in `lakana sql` and `lakana query wait` cancels the query they wait for, says what
  happened on stderr and exits with 130 (a second Ctrl-C while the cancel is sent exits at once and
  names `lakana query cancel ID`). The submitter and a holder of `MANAGE_ENVIRONMENT` may cancel;
  anyone else gets a 404. `query wait` cancels someone else's query too if you hold `MANAGE_ENVIRONMENT`. See <https://lakana-labs.github.io/lakana/how-to/stop-a-query/>.
- `lakana query list` shows your queries (everyone's with `MANAGE_ENVIRONMENT`): running first, then
  queued in the order they are taken, with the reason each waits; never the SQL or the error text.
  SDK: `client.queries.list(environment, include_ended=False, limit=100)` returns `QueryListEntry`
  objects.
- A query that is `QUEUED` says why: `lakana query status ID` shows `waiting_for`; `lakana sql` and
  `lakana query wait` print `Waiting: <reason>` on stderr after 3 seconds and again when the
  reason changes (nothing with `--output json`); `lakana runs show` has a `waiting_for` column
  for tasks. SDK: `status.waiting_for` is a `WaitingFor` (`code`, `message`, and the numbers
  `workers`, `ahead`, `other_workers`, `running`, `limit`, each `None` when the code does not use
  it) or `None`, and `run.wait(on_update=f)` calls `f` with every polled status. The codes include
  `no_worker_of_kind` (workers are running and none takes this kind of work: the operator starts
  one with `LAKANA_WORKER__TAKES=interactive`, `pipeline` or `all`) and `user_limit` (you already
  run as many ad-hoc queries as the limit per user allows: wait, or cancel one of them; `running`
  is your own number and a manager reading someone else's row gets `limit` only). Code that
  branches on `code` should treat an unknown one as "waiting". See
  <https://lakana-labs.github.io/lakana/how-to/why-is-it-waiting/>.
- In a protected environment, writes outside runs need `--break-glass --reason "…"` (SDK:
  `break_glass_reason=`) and `MANAGE_ENVIRONMENT`.
- `--env` picks the environment; without it, the profile's `default_environment` is used
  (`lakana login --env dev`, `lakana profile use NAME --env dev`) — unless `LAKANA_URL`/`LAKANA_TOKEN`
  supply the credentials, in which case pass `--env`. There is no built-in default.
- `--output json` (or `LAKANA_OUTPUT=json`) prints machine-readable JSON on stdout; errors go
  to stderr as `{"error": {"type", "message"}}` — usage errors included (type `UsageError`,
  exit 2), except a malformed global option, which Typer reports before the mode is known.
- Concurrent `lakana login` / `lakana profile` runs are safe: each update of the profile file
  holds an exclusive lock (`config.toml.lock`, next to it) for its whole read-modify-write, and
  gives up after 10 s if another process holds it. Reads never wait.
- There is no `--token` option. For CI, set `LAKANA_URL` and `LAKANA_OIDC=github` or
  `LAKANA_OIDC=gitlab`: `lakana deploy` then exchanges the job's ID token, and no token is
  stored. With a stored token, use `LAKANA_URL`/`LAKANA_TOKEN`, or
  `lakana login --url URL --token-stdin`; setting `LAKANA_TOKEN` and `LAKANA_OIDC` together is
  refused.
- Destructive commands ask first; `--yes` skips the prompt, and without a terminal they
  refuse unless `--yes` is given.

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | Unexpected error (`--verbose` for a traceback); a deploy or rollback that failed or outlived its `--timeout`; maintenance not applied because the table changed (retry); a downloaded artifact that did not match its manifest (`ArtifactCorrupt`, nothing of it is kept: download it again); or an answer from the server that is not the JSON the CLI expects (`InvalidResponse`: check the profile's URL) |
| 2 | Usage error: bad arguments, no environment, confirmation refused |
| 3 | Configuration: no credentials, or an unsafe profile file |
| 4 | Authentication failed (401) |
| 5 | Permission denied (403) |
| 6 | Not found (404) |
| 7 | Conflict (409) |
| 8 | Invalid input (422), or a local file/schema problem |
| 9 | Query failed or was cancelled by someone else, or a query's or maintenance run's `--timeout` elapsed |
| 130 | Ctrl-C in `lakana sql` or `lakana query wait`; the query was cancelled if the server could be reached |
| 10 | Payload too large (413) |
| 11 | Server error (5xx), connection failure, or a server too old for the command |

### Repositories

An admin (`MANAGE_REPOSITORY`) registers each git repository once; deployers name it.

```bash
lakana repo add analytics --kind https --url https://github.com/acme/analytics.git \
    --credential-secret github-token --default-ref main
lakana repo add infra --kind ssh --url git@gitlab.internal:data/infra.git \
    --credential-secret deploy-key --known-hosts-file ./known_hosts
lakana repo add demo --kind local --url ~/projects/demo   # under LAKANA_REPO_ROOTS
lakana repo list | show NAME | update NAME [--clear default_ref] | remove NAME --yes
```

`--credential-secret` names a secret, resolved in each target environment's scope at deploy
time: key `token` for `https`, `private_key` for `ssh`. Secret values never leave the server.
`repo remove` is refused while any environment has a current deployment from the repository;
`lakana undeploy` removes it first.

### Deploying a project

`lakana init [DIR]` scaffolds a project; see <https://lakana-labs.github.io/lakana/how-to/projects/> for assets, `lakana.yaml`, deploys and CI setup.

    lakana deploy [REPO] --env dev [--ref main] [--dry-run] [--no-wait]
    lakana rollback [REPO] --env dev (--to SHA | --to-previous)
    lakana deploys list --env dev | lakana deploys show ID --env dev
    lakana deployments --env dev
    lakana deployments set-run-as REPO --env prod (--principal P | --clear)
    lakana undeploy REPO --env dev [--yes]
    lakana project-version show ID --env dev
    lakana orphans --env dev

`REPO` defaults to `repo:` in the `lakana.yaml` found by walking up from the current directory
to the git root (outside a git repository only the current directory is checked). A deploy is fetched and validated by a worker, never by the CLI;
`lakana deploy` waits and prints the resolved SHA and the diff, exiting 1 if the deploy fails
(including losing a race to another deploy) or `--timeout` elapses (the run keeps going; the
message names the id for `lakana deploys show`). After a successful deploy it prints a hint on
stderr naming `lakana run [REPO] --env ENV` (REPO only when you named it on the command
line); there is none for `--dry-run`, `--no-wait`, a failed deploy,
a rollback or `--output json`. A rollback never contacts the git host. For a
`local` repository, the CLI warns when the work tree has uncommitted changes: only committed
state is deployed. Deploys record a project version; `lakana run` executes it (see "Running a project").

`set-run-as` binds the service account a protected environment's runs write as (needs
`MANAGE_ENVIRONMENT`). `undeploy` removes the deployment pointer and binding (needs `DEPLOY`,
refused while a run is active); tables and project versions stay.

### Running a project

    lakana run [REPO] --env dev [--select 'gold.*']... [--upstream] [--downstream] [--no-wait] [--timeout S]
    lakana runs list --env dev [--repo R] [--limit N] | lakana runs show ID --env dev
    lakana runs logs ID --env dev [--task ASSET] [--follow]
    lakana runs cancel ID --env dev | lakana runs resume ID --env dev
    lakana scheduler status
    lakana worker list

`lakana run` prints the run id (stderr), then a line per task each time its state changes
(`a.b  running  attempt 1/3`: the attempt in use out of the allowed attempts; no count before the
first attempt, and `retrying after attempt 1/3` while a failed task waits to try again), and
waits. Exit codes: 0 success; 1 failed,
cancelled or timed out; 7 a run is already active. `--select` globs match dotted table names and
`*` crosses namespace dots (see <https://lakana-labs.github.io/lakana/how-to/projects/>). SDK: `client.runs.*`, `client.scheduler.status()`. `lakana worker list` (admin) shows the registered
workers, `alive` or `lost`, the work each takes (`takes`: `all`, `interactive` or `pipeline`) and what
each is doing; SDK: `client.workers.list()` returns `WorkerInfo` objects, with `WorkerInfo.takes`.

### Jobs

    lakana job list --env dev [--repo R]
    lakana job run NAME --env dev [--repo R] [--no-wait] [--timeout S]
    lakana job pause NAME --env dev [--repo R]
    lakana job resume NAME --env dev [--repo R]
    lakana job notify-test NAME --env dev [--repo R]
    lakana job notification DELIVERY_ID --env dev

A job is a named selection in `lakana.yaml` (`jobs:`, see <https://lakana-labs.github.io/lakana/how-to/projects/>).
`job list` shows, per job, its state (`scheduled`, `paused`, `waiting`, `no schedule` or `no run
principal`), the schedule with its zone, the next run in the job's zone and in UTC, and the last
run with what started it; under the table, the sentence of each job that waits. `job run` starts the
job with the selection from the manifest and follows the run exactly as `lakana run` does (same
output, same exit codes, `--no-wait`, `--timeout`). It needs `RUN` on the environment and works also
on a paused job. When the project already has an active run, `job run` does not fail with 7 as
`lakana run` does: the start waits, begins when the project is free, and the command prints
`daily waits for run ID to finish; it starts then.` (and how to follow that run) and exits 0:
that means accepted, not run, so a script that needs the result follows the run with
`lakana runs show` (`--output json` has `waiting` instead of `run`). Only one start waits per job; another start merges into it. The trigger of the
run is `api` when the caller is a service account and `manual` otherwise. `job pause` and `job
resume` need `DEPLOY` on the environment: resume computes the next run from now, so the times
missed while paused do not fire; a pause keeps a waiting start. A paused job shows no next run in `job list`. The state
`waiting to start` means a start waits and no run is active, so the scheduler begins it within
seconds; a waiting start is checked again then, and one whose requester was deactivated or lost
`RUN`, or that has no run principal any more, is dropped with an audit event
(`JOB_START_DROPPED`). `--repo` defaults to `lakana.yaml`'s
`repo:`. `lakana runs list|show` have `job` and `trigger` columns (`show` also `scheduled_for` and
`missed`). Exit codes as elsewhere: 5 without the permission, 6 for an unknown job (the message lists
the jobs there are), 7 when a protected environment has no run principal, or when you pause a job that has no
schedule. SDK: `client.jobs.list|start|pause|resume`, which return `Job` and
`JobStart` (`run` or `waiting`).

`job notify-test` queues a test message (clearly fake run data) to the webhook the job's `notify:`
names, then reads the delivery once a second for up to 30 seconds and prints the result:
`delivered` (exit 0); `failed: <reason>` with the next step, for example "the address is private;
allow it with LAKANA_WEBHOOK__ALLOW_HOSTS" (exit 1); or `still trying (attempt N of 5); see
'lakana job notification ID --env dev'` (exit 1: the result is not known, so a script must not
treat it as success; `--output json` carries the `state`). It needs `DEPLOY` on the environment.
A job without `notify:` gives exit 7 ("add or fix it in lakana.yaml and deploy"), an unknown job exit 6.
`job notification` shows one delivery (needs `DEPLOY`, `RUN` or `READ_ENVIRONMENT`). `lakana runs
show` lists the notifications of a run under its tasks, each with its delivery id: `delivered`,
`sending (attempt 1 of 5)`, `retrying (attempt 2 of 5); last: ...` or `failed: the server answered
404`. SDK: `client.jobs.notify_test(env, repo, job)` returns the
delivery id, `client.jobs.delivery(env, id)` a `Delivery`, and `Run.notifications` the list (empty
from an older server).

### Python assets

SQL transforms, Python at the edges: a Python asset receives its declared inputs as polars
DataFrames and returns the DataFrame that replaces its table; see <https://lakana-labs.github.io/lakana/how-to/projects/>, "Python
assets", for the rules, what a deploy checks and what a task may touch. With `output="artifact"` it
stores a file or a directory (a trained model, say) instead; see "Artifacts" below. The project also needs a
`pyproject.toml` that depends on `lakana[assets]` and a committed `uv.lock`.

```python
# assets/gold/station_scores.py  ->  table gold.station_scores
import polars as pl
from lakana import asset


@asset(inputs={"readings": "bronze.readings", "stations": "bronze.stations"}, retries=2)
def station_scores(readings: pl.DataFrame, stations: pl.DataFrame) -> pl.DataFrame:
    """Temperature spread per station over the last week of readings."""
    recent = readings.filter(pl.col("ts") > pl.col("ts").max() - pl.duration(days=7))
    scored = recent.group_by("station_id").agg(score=pl.col("temp_c").std())
    return scored.join(stations, on="station_id")
```

The inputs are tables other assets build (here two SQL assets, `bronze.readings` and
`bronze.stations`). The full walk-through, with those two files, the `pyproject.toml` and the
deploy steps, is in <https://lakana-labs.github.io/lakana/how-to/projects/>, "Python assets".

Python assets run as processes of the worker's user, not in a sandbox: one instance is one trust
domain, so deploy only code you would run on the worker yourself.

### Artifacts

An artifact is a named file or directory, such as a trained model, stored in numbered versions and
governed like a table. An asset declared `@asset(output="artifact", keep_versions=5)` writes into
`lakana.output_dir()` and returns a `pathlib.Path` inside it; a consuming asset gets a `Path` to a
read-only copy. Everything is in <https://lakana-labs.github.io/lakana/reference/artifacts/>; the SDK is `client.artifacts`.

    lakana artifact list [--namespace NS] | upload NAME PATH | download NAME DIR [--version N]
    lakana artifact versions list NAME | versions delete NAME N [--yes]
    lakana artifact drop NAME [--yes] | uploads list [NAME] | uploads abort NAME UPLOAD_ID

`upload`, `versions delete` and `drop` take `--break-glass --reason "…"` in a protected
environment. Loading a pickled model runs code: see
<https://lakana-labs.github.io/lakana/explanation/artifacts/>, "A pickled model is code".

### Data quality checks

A project keeps checks under `checks/<namespace>/<table>/<name>.sql`: a `SELECT` that returns the
rows that are wrong. A run counts them after it writes the table; a failing blocking check sets
the table back and stops what reads it. A guide is <https://lakana-labs.github.io/lakana/how-to/check-your-data/>, the
rules are in <https://lakana-labs.github.io/lakana/reference/checks/>.

    lakana check list --env dev [--run RUN_ID] [--table NS.TABLE] [--limit N]
    lakana runs show RUN_ID --env dev        # the checks of each task, after the task table
    lakana deploy --env dev --dry-run        # lists the checks under "Checks:"

SDK: `client.checks.for_run(env, run_id)`, `client.checks.list(env, table=..., cursor=...)` and
`client.checks.iter(env, table=...)` return `CheckResult` objects (a count of rows, never a row).
`RunTask.checks` holds a task's results and `RunTask.kind` is `asset` or `source_check` (a table
the project only reads). `Deploy.checks` lists a project's checks by table. `Table.state` is
`published` or `not_published`, with `Table.state_message` saying why; a read of a table that is
not published is refused.

## Local mode

`pip install "lakana[server]"` also installs `lakana-server` and `lakana-worker`, so `lakana server
start --local` can run a complete Lakana on this machine: SQLite for the catalog, the local
filesystem for storage, no Docker and no Postgres.

```bash
pip install "lakana[server]"
lakana server start --local          # runs in the foreground; no line per request
lakana sql "SELECT 42"               # in another terminal, once it's ready
lakana server status                 # exit 0 if running, 1 otherwise
lakana server stop                   # same as Ctrl-C in the instance's terminal
```

`--log-requests` on `start` prints a line for every request (local mode sets `LAKANA_LOG__ACCESS=false`
otherwise); `LAKANA_LOG__LEVEL` sets the level for the server and the worker as before. `lakana server stop` asks the instance in the
data directory (`--data-dir DIR` for another one) to shut down and waits up to 30 seconds; nothing
running is not an error, it exits non-zero when the instance is still there after the wait (it never
kills it), and it is refused for a remote or Compose instance (`docker compose down` stops those).

A `--kind local` repository must lie under one of `LAKANA_REPO_ROOTS`; local mode defaults that to
your home directory (resolved), so `lakana repo add demo --kind local --url "$PWD"` works anywhere
under `$HOME` and is refused elsewhere (`/tmp`, another mount); the error says to move the
project under your home directory, or, on a server, that the operator sets `LAKANA_REPO_ROOTS`
(it never prints the configured roots). To deploy from another place, start with `LAKANA_REPO_ROOTS='["/srv/projects"]' lakana server start --local`.

`lakana[assets]` adds polars for projects with Python assets: `python -m lakana.runner`, the
harness that runs one, needs it. The SDK and CLI work without it.

The `server` extra pins `lakana-server` and `lakana-worker` to `lakana`'s own exact version: a
`hatch_build.py` metadata hook (`[tool.hatch.metadata.hooks.custom]`) sets
`optional-dependencies.server` from `lakana`'s already-resolved `hatch-vcs` version at build
time, so `pip install "lakana[server]"` always installs matching versions of all three, never
a mix. A dev build off an unreleased commit (`lakana==X.Y.Z.devN+gHASH`) pins the same local
version string, which only resolves against a matching local wheel or an index that carries
it, not a plain PyPI release. `lakana-server` and `lakana-worker` each carry their own
`hatch_build.py` metadata hook that pins their `lakana-core` dependency to the same
build's version too, so a `lakana[server]` install always resolves one matching
`lakana-core` rather than whatever the latest release happens to be.

The data directory defaults to `$XDG_DATA_HOME/lakana/local` (`~/.local/share/lakana/local`),
overridable with `--data-dir`; `--port` overrides the default port `8765`. If the port is in use,
another program or a Lakana instance started with a different `--data-dir` has it
(`lakana server status --data-dir DIR` checks an instance); pass another `--port`: `start` points
the `local` profile at it, so later commands find the instance without `--url` (add
`--profile local` if another profile is your default). It holds
`lakana.db` (SQLite, WAL mode), `lake/` (storage root), `master.key` (mode `0600`), the
running instance's pidfiles and `.lakana-local-instance`, the marker that `lakana server reset`
requires. On first start, a `local` profile is saved automatically
(`default_environment: dev`) and becomes the default profile if none exists yet.

`lakana server start --local` binds only to `127.0.0.1` — it is never reachable from another
machine, whatever `--port` you pick.

To start completely over, stop the instance (`lakana server stop`, Ctrl-C, SIGTERM, or — on POSIX — closing the
terminal, which sends SIGHUP) and run `lakana server reset --local` (`--data-dir DIR` for another
data directory, `--yes` to skip the question). It names what it deletes (`lakana.db` and its
SQLite side files, `lake/`, `master.key`, the pidfiles, and the number of tables) and asks
first; the next `start` is a first run with a fresh `master.key` and a fresh bootstrap
credential. It never touches your projects. It works on this machine's instance only (it
refuses a remote `--url` or profile, and a Compose deployment). It refuses while the instance
runs, if the data directory is a symlink, a filesystem root, your home directory or a folder
that holds it, if anything it would delete is a symlink, and if the directory lacks the file
`.lakana-local-instance`, which every `lakana server start --local` writes into its data
directory. A data directory that an older `lakana` created has no such file: start the instance
once with the current version, stop it, and run the reset.

If a first run is interrupted after the `local` profile is saved but before its `dev`
environment is created, the next `start` notices `dev` is missing and creates it itself (if that
check fails, e.g. the profile's token is stale, `start` warns and keeps running); to
do it by hand instead, `lakana env create dev --storage-root file://<data-dir>/lake/dev` (not
`lakana environment create`, which isn't a command).

| `start` exit code | Meaning |
|---|---|
| 0 | Shutdown completed (Ctrl-C/SIGTERM/SIGHUP), whether or not a straggler needed `SIGKILL` after the 10 s grace period |
| 1 (or the child's own code) | Migration failed or the server didn't answer `/healthz` in time (1); a child exited after startup (its exit code, at least 1) |
| 2 | `--local` is missing, an instance is already running from this data directory, the port is in use, or the data directory/`master.key` is unsafe (a symlink, another user's, or group/other-accessible) |
| 3 | The `lakana[server]` extra isn't installed, or the data directory/`master.key`/a pidfile couldn't be prepared, read or written |

| `status` exit code | Meaning |
|---|---|
| 0 | An instance is running |
| 1 | No instance is running from this data directory |

## Team mode: `lakana bundle init`

For a small team sharing one instance instead of running locally, `lakana bundle init`
generates a Docker Compose bundle — Postgres for the catalog, SeaweedFS for object storage,
the server, one or more workers, and a one-shot setup job:

```bash
lakana bundle init
cd lakana-deploy && docker compose up -d --wait
lakana sql "SELECT 42"
```

It writes `compose.yaml` and `.env` (mode `0600`, holding every generated secret) into
`./lakana-deploy` by default, and refuses to overwrite an existing bundle rather than risk
rotating the secrets master key out from under one. It also saves a profile — named `team`
by default, from the global `--profile` option — so the CLI can talk to the new instance
immediately. By default the server binds to `127.0.0.1` only; `--bind 0.0.0.0` (or a
specific address) exposes it on the network over plain HTTP, with a warning, and
`--public-url` points the saved profile at a reverse proxy instead.

See [the Compose deployment guide](https://lakana-labs.github.io/lakana/how-to/deploy-with-compose/) for the full reference:
every `.env` key, exposing it with TLS, scaling workers, upgrading, backups, and revoking
the bootstrap credential.
