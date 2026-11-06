# Is my instance healthy?

This guide is for the person who runs a Lakana instance. It lists what to look at to tell
whether the instance works, the command or address that answers each question, and what a bad
answer looks like. The commands are for the Compose bundle ([Deploying with Compose](deploy-with-compose.md));
`lakana worker list` and the others also work against any other instance you have an
administrator's access to.

After this guide you can check the services, the workers, the queue, the catalog database, the
object storage and the disk, you can read the one line a stuck or failed run gives you, you know
which lines of the logs matter, and you know what to collect before you ask a question in public.

## The short version

Run these in order. The first that looks wrong is where to read on.

1. `docker compose ps --all`: every service is `running` and `healthy`, and the one-off services
   (`migrate`, `bucket`, `setup`) have `Exited (0)`. ([The services](#the-services))
2. The address `/readyz` answers `{"status":"ready"}`. ([The health routes](#the-health-routes))
3. `lakana worker list`: as many workers are `alive` as you started. ([The workers](#the-workers))
4. `lakana query list --env dev --status queued` does not grow. ([The queue](#the-queue))
5. `lakana env usage` and the disks have room. ([Disk use](#disk-use))

Nothing here is a service level, and none of it replaces looking at what your own people notice:
a dashboard that is slow or a job that did not run is a signal too.

## The services

From the directory of the bundle:

<!-- not tested: asks Docker Compose about the running stack, and the example runner has no Docker -->
```bash
docker compose ps --all
```

What each line tells you:

| Service | Healthy means | It does not mean |
|---|---|---|
| `postgres` | The database accepts connections on its network address. | That the catalog inside it is intact, or that it has room left. |
| `seaweedfs-core` | The storage's master answers and its file service accepts connections. | That the volume has room left. |
| `seaweedfs` | The S3 gateway, the only way to the data, answers HTTP. | That the credentials and the `lakana` bucket work. |
| `server` | The server answers `/healthz` (next section). | That the database, the storage or any worker works: `/readyz` says that. |
| `worker` (and `worker-interactive`, `worker-pipeline`) | The worker of the container wrote its heartbeat to the database in the last 60 seconds, so it reaches the database. | That it takes work, or that it can reach the storage. |

The one-off services `migrate`, `bucket` and `setup` run once and end; `Exited (0)` is their
healthy state. One that exited with another number is the reason the services behind it did not
start: read its log with `docker compose logs migrate` (or `bucket`, `setup`). A service that
restarts again and again shows `Restarting`; its log says why.

## The health routes

The server has two health routes. Neither needs a sign-in, so a script, a load balancer, a
monitoring system or Docker can ask them. `$URL` is the public address of the instance.

**`/healthz`: the server process answers.**

<!-- source: docs/examples/getting-started/run.sh#healthz -->
```bash
curl -s "$URL/healthz"
```

<!-- output: getting-started#healthz -->
```text
{"status":"ok"}
```

That proves one thing: the server process is running and answers HTTP. It does not look at the
database, the storage or the workers. The `server` service's healthcheck asks this route from
inside the container every 10 seconds, which is what its `healthy` in `docker compose ps` comes
from.

**`/readyz`: the instance can do work.**

<!-- source: docs/examples/getting-started/run.sh#readyz -->
```bash
curl -s "$URL/readyz"
```

<!-- output: getting-started#readyz -->
```text
{"status":"ready"}
```

It answers `200` and `{"status":"ready"}` when all three of these hold, and `503` with the ones
that do not, for example `{"status":"not_ready","failed":["worker"]}`:

| Name | Holds when | When it fails, look at |
|---|---|---|
| `database` | The catalog database answers a query within 2 seconds. When it fails, `storage` and `worker` are listed too: both are read from the database. | [The catalog database](#the-catalog-database-and-the-object-storage). |
| `storage` | The storage of every environment answers within 2 seconds and accepts its credentials (at most the first 5 environments by name). Nothing is read or written. | [The object storage](#the-catalog-database-and-the-object-storage). |
| `worker` | At least one worker wrote its heartbeat in the last 60 seconds (`LAKANA_RUN__LEASE_SECONDS`). | [The workers](#the-workers). |

The answer names the checks and nothing else: no address, no environment, no error. The server
log has the reason, in one line when the answer turns to `503` and one when it is back
(see [Log lines that matter](#log-lines-that-matter)). One answer is reused for 5 seconds, so a
caller that asks often does not load the database or the storage. A brand-new instance answers
`503` until `setup` has run and the first worker has started.

Three settings change it, on the `server` service ([all settings](../reference/settings.md)):

- `LAKANA_READINESS__ENVIRONMENTS`: the environments whose storage counts, as a JSON list such
  as `'["dev","prod"]'`. Set it when an environment's storage is out of reach from the server on
  purpose, or when you have more than 5 environments.
- `LAKANA_READINESS__TIMEOUT_SECONDS` (2) and `LAKANA_READINESS__CACHE_SECONDS` (5).

Point a load balancer's or a monitoring system's check at `/readyz`, not at `/healthz`. Do not
make the bundle's own `server` healthcheck ask `/readyz`: the workers start only after the server
is healthy, so it would never become healthy.

A server that does not answer at all is a stopped or restarting container, a proxy or a firewall
in front of it, or a wrong address. `docker compose ps` and `docker compose logs server` tell
which.

## The workers

A worker is the process that runs queries and tasks. It reports in to the database every few
seconds. It is `alive` while its last report is recent, and `lost` once it has been silent for
60 seconds (`LAKANA_RUN__LEASE_SECONDS`). A lost worker's row stays for an hour, and what it was
running is ended by the worker that holds the scheduler role. The commands need the right to
manage environments on the instance, which an administrator has:

<!-- source: docs/examples/getting-started/run.sh#worker-list -->
```bash
lakana worker list
```

<!-- source: docs/examples/getting-started/run.sh#scheduler-status -->
```bash
lakana scheduler status
```

Healthy looks like this:

- **As many `alive` rows as you started.** `LAKANA_WORKERS` plus `LAKANA_WORKERS_INTERACTIVE`
  plus `LAKANA_WORKERS_PIPELINE` in the bundle's `.env`. Fewer means a worker did not start or
  cannot reach the database; a row with the status `lost` is one that stopped reporting.
- **Every kind of work you use has an `alive` worker.** The column `takes` says `all`,
  `interactive` or `pipeline`. See [Seeing that a kind has no worker](deploy-with-compose.md#seeing-that-a-kind-has-no-worker).
- **The workers take work.** The column `doing` shows `idle` or the query, deploy or task a worker
  runs. An `alive` worker that stays `idle` while queries wait is the case to look into: the
  reason of each waiting query says why (see [A stuck run](#a-stuck-run-and-why)).
- **One worker holds the scheduler role.** `lakana scheduler status` names it, and when it last
  reported. The role ends what lost workers held and runs the hourly sweep, so a holder that has
  not reported for much longer than a minute, or no holder, is a problem:
  [When a worker is lost](why-is-it-waiting.md#when-a-worker-is-lost) says what frees it.

An empty list prints `No worker is registered.` followed by what to do. With the Compose bundle
that is `docker compose logs worker` first.

## The queue

A healthy instance has queries and runs that start soon after they are submitted. Two listings
show what waits; run them now and again after a few minutes. A queue that is short and changes is
normal; one that only grows is not.

<!-- source: docs/examples/getting-started/run.sh#queue-length -->
```bash
lakana query list --env dev --status queued
```

<!-- source: docs/examples/getting-started/run.sh#open-runs -->
```bash
lakana runs list --env dev --status queued,running
```

Use the name of your own environment instead of `dev`. You see your own queries; with
`MANAGE_ENVIRONMENT` on the environment you see everyone's. The `waiting_for` column of the query
listing gives the reason each one waits. To see what failed in the last day, use this listing (on macOS, write `date -u -v-1d +%Y-%m-%dT%H:%M:%SZ`
instead of the `date` command shown):

<!-- source: docs/examples/getting-started/run.sh#find-failed-run -->
```bash
lakana runs list --env dev --status failed --since "$(date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%SZ)"
```

More filters and paging: [Find a run, a query or a deploy](find-a-run.md).

## The catalog database and the object storage

Two things the instance cannot work without. `/readyz` checks both from the server
([The health routes](#the-health-routes)); this is how to read them from the rest of the instance.

**The catalog database.** `docker compose ps` shows `postgres` as `healthy` when it accepts
connections. A command that reads the catalog, such as `lakana worker list`, answers only when
the server reaches the database, so an answer proves it. When the database goes away:

- every request that needs it fails, `/readyz` answers `503` with `database`, and a new server
  container does not start (the `migrate` service runs first and exits with an error);
- each worker's container turns `unhealthy` within about half a minute after its heartbeat is a
  minute old;
- each worker logs `The worker's heartbeat could not be written; it is tried again every 5 s.` (the line ends `event=database_unreachable`)
  and, after a minute without success, `No heartbeat was written for 60 s (LAKANA_RUN__LEASE_SECONDS): this worker claims nothing and stops the query it runs until one is written. Check that the database is reachable. event=heartbeat_missed`
  Its queries and deploys are then ended as lost. When the database is back, it logs
  `The worker's heartbeat is fresh again; it claims work again. event=heartbeat_recovered`

**The object storage.** `seaweedfs` being `healthy` shows the gateway answers, and `/readyz`
without `storage` shows that the server reaches each environment's storage with its credentials.
That the workers can read and write your data you learn by doing it: run a query that reads a
table of yours. The tutorial's query is an example; use a table you have:

<!-- source: docs/examples/getting-started/run.sh#query -->
```bash
lakana sql "SELECT * FROM gold.station_ranking ORDER BY warmth_rank"
```

When the storage cannot be reached, a query that reads a table fails with
`The storage of environment 'dev' could not be reached, so no table was read.` and says to try
again and to tell the operator; a request that stores or sends an artifact file answers that the
storage could not be written or read and that the operator should check whether it is
unreachable. Read `docker compose logs seaweedfs` and `docker compose logs seaweedfs-core`.

## Disk use

Two places fill up: the object storage, with your tables and artifacts, and the disk under the
workers' cache, with Python environments and downloaded packages. `lakana env usage` shows both
(it needs the right to manage the environment; the workers' part, the right to manage
environments on the instance, which an administrator has):

<!-- source: docs/examples/getting-started/run.sh#env-usage -->
```bash
lakana env usage dev
```

Leave out `dev` to see every environment you manage. The first table has one row per environment:

| Column | What it counts |
|---|---|
| `tables` | The managed tables: the files of each table's current version, and how many tables. |
| `dropped` | The dropped tables you can still restore; their files stay until the undo period ends (7 days by default). |
| `artifacts` | The files stored for the artifacts. |
| `code` | The code archives of the deployed project versions. |
| `total` | The four together. |

The figures come from the catalog database, never from a walk over the storage, so the command is
quick however large the bucket is. The sizes of tables are measured by the worker's hourly sweep:
a table written since then is counted with its earlier size, and one never measured not at all;
the cell then says how many are `unmeasured`, and the total ends with `+`, a floor. After an
upgrade the first sweeps measure the tables that existed before, up to 200 per sweep. Code archives
of deploys made before this release have no recorded size and stay `unmeasured`. Not counted: the
files of older table versions that [maintenance](../reference/table-health.md) has not removed yet,
the tables' metadata files, external tables (their files are yours, not Lakana's) and files
nothing names ([Clean up files left by older releases](clean-up-files-left-by-older-releases.md)).

The second table has one row per worker: its Python environments, uv's package download cache and
the working directories of running tasks, as the worker measured them in its last sweep, and when.
`-` means the worker has not measured yet (it does shortly after it starts). `lakana worker
list` shows the same total in its column `caches`. Workers that share one cache volume, as the
replicas of the Compose bundle do, show the same figures: do not add them up. The package cache
leaves out the files it shares with an environment, so its figure is what trimming it can free at
most.

**The package cache trims itself.** Above `LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES` (10 GiB by
default; `0` never trims) the worker's sweep removes what no package refers to any more, then the
packages downloaded longest ago, never one downloaded within
`LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS` (24 by default). It never runs while the worker builds
an environment, and when another worker that shares the cache is building, it tries again at the
next sweep. The environments that exist keep working; the next build that needs a removed package
downloads it again. Each trim logs `The package cache was trimmed from … to … bytes`
(`event=package_cache_trimmed`). A trim that leaves the cache above the limit logs a warning that
says why (`event=package_cache_over_limit`): every package left is recent, a `uv cache` command
failed, or the trim ran out of time. Then raise the limit, give the volume more room, or lower the
minimum age. The Python environments themselves are limited by count
(`LAKANA_RUN__VENV_KEEP`, 10), and the task directories are removed when their task ends.

**The disks themselves.** Lakana sends no alert when a disk fills, so look at the disks too. The
bundle keeps its state in three named Docker volumes, all on the disk that holds Docker's data:

| Volume | Holds | What fills it |
|---|---|---|
| `pgdata` | The catalog (PostgreSQL). | Table, run and audit history. |
| `seaweed` | The data of every table and every artifact. | Your data: the `total` of `lakana env usage`, plus older table versions. |
| `worker-cache` | At `/home/lakana/.cache/lakana-worker`: the Python environments of the projects, the download cache of `uv`, and the working directories of the tasks that run. | Every package version a project locks; the download cache is trimmed above its limit. |

The worker containers also keep temporary files under `/var/tmp/lakana`, inside the container.
The sweep removes what a worker that was killed left behind, once an hour
(`LAKANA_RUN__SWEEP_INTERVAL`; `0` turns every sweep off, and then nothing is measured or trimmed).

<!-- not tested: asks Docker for the disk use of a running stack, and the example runner has no Docker -->
```bash
docker system df -v
docker compose exec worker df -h /home/lakana/.cache/lakana-worker
```

The first command lists every volume with its size. The second shows how full the disk under
the workers' cache is. Also look at the disk of the machine itself with `df -h` on the host.
The documentation gives no figure for how much space your data needs; measure on your own data.

## Which task needs the most memory

Set a memory limit for a task, or find the one that grows, from what its attempts used.
`lakana task history JOB TASK` lists the newest attempts of one task of one job across all its
runs, newest first:

<!-- source: docs/examples/run-a-job/run.sh#task-history -->
```bash
lakana task history hourly_bronze bronze.readings --env dev --last 10
```

<!-- output: run-a-job#task-history -->
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┓
┃ run                                  ┃ attempt ┃ status    ┃ duration ┃ resources ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━┩
│ 87189d2e-caab-4e49-953f-d9f53a1fb143 │ 1       │ succeeded │ 1.0s     │ —         │
└──────────────────────────────────────┴─────────┴───────────┴──────────┴───────────┘
```

- **`run` and `attempt`**: the run the attempt belongs to (`lakana runs show RUN_ID` shows the
  run) and its number; a task that was retried has several attempts in one run.
- **`status`**: how the attempt ended. A failed attempt names why, as in `failed (memory)`
  ([Why did it fail?](../reference/why-did-it-fail.md)); one that has not ended is `running`.
- **`resources`**: the highest memory the attempt reached, its processor time and the bytes it
  read, for example `412 MiB · 3.1 s CPU · 1.2 GiB read`. A part that was not measured is left
  out, and a row with no part at all shows `—`. For a Python task in a container the memory and
  processor time come from the container's cgroup, read by the launcher about four times a second:
  they are at least that much (growth after the last reading is not counted), a task that ends
  before the first reading can show `—`, and so does every task where the launcher cannot read the
  host's cgroup tree
  ([Peak memory and processor time of a task](run-python-tasks-in-containers.md#the-launcher)).

A task whose memory climbs run over run reads from the bottom of the table upwards: the
`resources` of each newer row is larger than the one before, while the data it reads grows too.
That is the time to raise its limit, or to read less of the data per run, before an attempt
fails with `memory`.

`--last` sets how many attempts to show (1 to 200, 20 by default); when older ones exist the
command says so. Name the repository with `--repo` when two repositories have a job with the
same name, and the environment with `--env`. `--output json` prints every figure for a script.

A `—`, or a missing part, means nobody measured it. Find the cause in this list; each has a
next step:

- **The attempt ran before the upgrade that brought the figures.** Older attempts stay `—`; there
  is nothing to do.
- **A Python task in a container, and the launcher is older than the worker.** The launcher
  reports no figures. Upgrade the launcher together with the workers.
- **A Python task in a container, and the launcher cannot read the host's cgroup tree.** The
  launcher needs the read-only mount `/sys/fs/cgroup:/host/cgroup:ro` and
  `LAKANA_LAUNCHER__CGROUP_ROOT=/host/cgroup`. A Compose bundle from an earlier release has
  neither: add both ([Peak memory and processor time of a task](run-python-tasks-in-containers.md#the-launcher)).
- **A Python task has no memory or bytes read, and its project was locked before this release.**
  The task reports them through the `lakana` package locked in the project. Lock the project
  again (`uv lock --upgrade-package lakana`, so that it names this release) and deploy it.
- **A Python task on macOS.** Its peak memory is not measured there yet; the processor time and
  the bytes read are.
- **The task was shorter than the first reading.** A container task that ends before the
  launcher's first reading has no memory figure; a Python task that was killed has no count of
  bytes read.

For a Python task, `read` counts what it took in through `lakana`: the files, REST pages and
database results of `lakana.ingest` and the tables it reads with the SDK. A task that reads with
its own library (its own HTTP client, `pandas.read_csv` on a path) shows no bytes read, and `0 B`
means a Lakana reader ran and found nothing. Its memory is the task process's own peak on Linux. A task the project declares that has not run yet
has no attempts: the command says so and suggests starting the job. The command answers 404 only for a job or task name the project does
not declare and no run has attempted: check both names (`lakana job list` shows the jobs).

Past the newest `--last` attempts, the command prints a `--cursor` that continues after them;
with `--output json` the `next_cursor` field carries it.

## A stuck run and why

A query or a task that has not started says why, in one sentence, and a task that failed has a
code, a cause and a next step. Both are shown where you look at the run:

- **Waiting.** `lakana query status QUERY_ID --env dev` shows `waiting_for` for a queued query,
  and `lakana runs show RUN_ID --env dev` shows it for each task that waits. Most reasons pass by
  themselves. The ones that need you are `no_worker` and `no_worker_of_kind` (start a worker),
  and a reason that does not change for a long time. All of them, and what to do, are in
  [Why is my query or task waiting?](why-is-it-waiting.md).
- **Failed.** `lakana runs show RUN_ID --env dev` shows the code of each failed task and under the
  table the cause and `Next:`. The codes that point at the instance, rather than at a project, are
  these; all of them are in [Why did it fail?](../reference/why-did-it-fail.md):

| Code | What it says about the instance |
|---|---|
| `worker_lost` | The worker running the task stopped before it finished: it was killed, lost power or its network, or was stopped for memory. Look at the worker's log and memory limit. |
| `memory` | The task used more memory than the worker allows. Raise `LAKANA_WORKER_MEMORY` or make the task handle less data: [Memory limits](deploy-with-compose.md#memory-limits). |
| `no_runtime` | This worker cannot run this kind of task, for example because it has no Python runtime. The detail names what is missing. |
| `code_changed` | The deployed code is missing or no longer matches what was deployed. Deploy the project again; if it repeats, check the storage. |
| `internal` | The platform failed in a way it did not expect. The worker's log has the traceback. |

## Log lines that matter

Lakana's own records go to standard error, one per line with the time, the level and the logger
name; the web server's lines (startup and, if switched on, one per request) have a shorter form
without the time. `docker compose logs` shows both together. Read them with `docker compose logs server` and `docker compose logs worker`; the default
level, `INFO`, shows all of the lines below. How to change the level and what the lines of the
sweep mean: [The sweep](deploy-with-compose.md#the-sweep) and
[After a restore](deploy-with-compose.md#after-a-restore).

The lines worth an alert end with a stable code, `event=<code>`: match your alert rules on it,
not on the wording. Every code, its process and what to do: [Log events](../reference/log-events.md).

| Where | The line starts | Code | What it means and what to do |
|---|---|---|---|
| worker | `Memory limit exceeded: the worker and its tasks use` | `memory_stop`, `memory_task_ended` | The worker passed 90 percent of its memory limit and ended its largest query or task, or stopped itself. Narrow the work or raise `LAKANA_WORKER_MEMORY`: [Memory limits](deploy-with-compose.md#memory-limits). |
| worker | `The worker's heartbeat could not be written` | `database_unreachable` | The worker cannot reach the database (see above). `The worker's heartbeat is still failing` repeats it, and `No heartbeat was written for` (`heartbeat_missed`) says the worker has stopped claiming work. |
| worker | `Task run <id> is no longer ours; its outcome is dropped.` | `lease_lost` | The worker's claim on a task had run out when the task finished, so the worker dropped the result. Look for `worker_lost` on the run. |
| worker | `Lease renewal of task run <id> failed; retrying.` | `lease_renewal_failed` | The worker could not extend its claim on a task. If it keeps failing, the task is taken from it. |
| worker | `Polling iteration failed; continuing.` | `poll_failed` | The worker's loop that looks for work hit an error, followed by the error itself. Occasional lines are harmless; a stream of them is not. |
| worker | `Running task run <id> failed unexpectedly.`, `Deploy run <id> failed unexpectedly.` | `internal_error` | The platform failed in a way it did not expect. The run shows the code `internal` and says to see the worker log; the traceback follows the line. |
| worker | `Python assets: disabled:` | none | At start-up: this worker cannot run Python tasks; the line ends with `To enable it:` and the fix. |
| server | `The instance is not ready: /readyz answers 503.` | `readiness_failed` | The readiness check turned to failing; the line says which check and why. `readiness_recovered` says it is back. |
| server | `The grants that have no effect could not be listed at start-up (a database error)` | none | The database answered with an error while the server started. Start-up goes on; check the database. |
| server | A start-up that stops with a message that names a setting | none | A setting has a value the program does not accept. The message names it; correct it in `compose.yaml` or `.env`. |
| server | An access line that ends `500 Internal Server Error` or `503` | none | The server could not answer a request (a 503 asks the client to try again, for example when the storage could not be opened). The lines around it have the cause. |

## What to collect before you ask

Questions and bug reports go on a public page ([Support](../explanation/support.md)), where
anyone can read them, so collect what lets a stranger understand your case and nothing that lets
a stranger into your instance. In this order:

**1. The versions.** The CLI's and the instance's:

<!-- source: docs/examples/getting-started/run.sh#version -->
```bash
lakana --version
```

**2. The state of the services.** The output of `docker compose ps --all`, and the image you run:
the `LAKANA_IMAGE` line of `.env`.

**3. The failing run or query.** Its id and, for a run, the code from `lakana runs show`. Not the
SQL or the data if either is private.

**4. The relevant log lines**, with secrets removed. This writes the last hour of both logs to a
file and replaces two kinds of secret: an access token (it starts `lakana_pat_`; the server never
logs one itself, but a tool or a person may have pasted one), and a password in a connection
address:

<!-- not tested: reads the logs of a running Docker Compose stack, and the example runner has no Docker -->
```bash
docker compose logs --no-color --since 1h server worker \
  | sed -E -e 's/lakana_pat_[^[:space:]]+/lakana_pat_REDACTED/g' \
           -e 's#(://[^:/@[:space:]]+:)[^@[:space:]]+@#\1REDACTED@#g' > lakana-logs.txt
```

Open the file and read it before you post it: the filter knows two patterns, not your data. A
table name, a file path, a person's name or a SQL statement can be in a line, and a package index
address can carry a credential (`user:token@` in its address). Cut a line or replace a word when
in doubt.

Never post the `.env` file, the output of `docker compose config` (it prints the values of
`.env`), a token, the master key, or the storage keys. If you posted one by mistake, treat it as
public: replace the token ([Give a second person access](give-a-second-person-access.md#take-access-away)),
and for the master key and the storage keys start with [Manage secrets](manage-secrets.md).

Lakana is run by one maintainer, and a question gets an answer when time allows or not at all;
[Support](../explanation/support.md) says where to ask and what the project does and does not
provide. A report with these four things in it is one that can be understood without asking
you for more.

## See also

- [Run Lakana for other people](operate-for-others.md): the whole job of an operator, in order.
- [Why is my query or task waiting?](why-is-it-waiting.md) and [Why did it fail?](../reference/why-did-it-fail.md).
- [Deploying with Compose](deploy-with-compose.md): the services, the memory limit, the sweep and the logs.
- [Log events](../reference/log-events.md): the codes of the log lines worth an alert.
- [The audit log](../explanation/security.md#what-the-audit-log-records): who did what.
