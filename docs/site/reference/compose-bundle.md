# The Compose bundle

The files and settings of the bundle `lakana bundle init` writes. The steps to run it are in
[Deploying with Compose](../how-to/deploy-with-compose.md); every `LAKANA_*` variable of the server and
the workers, with its type and default, is in the [settings reference](settings.md).

## The `.env` file

`.env` has mode `0600`, because it holds every secret this deployment has. Its keys:

| Key | Purpose |
|---|---|
| `LAKANA_IMAGE` | The Lakana image reference (`ghcr.io/lakana-labs/lakana:<version>` by default — see [Upgrading](../how-to/deploy-with-compose.md#upgrading) and [`--image`](../how-to/deploy-with-compose.md#what-init-generates)). |
| `LAKANA_TASK_IMAGE` | The task images' reference without `-py3.X` (`ghcr.io/lakana-labs/lakana-task:<version>` by default, see [`--task-image`](../how-to/deploy-with-compose.md#what-init-generates)); the launcher runs a project's Python tasks on `<reference>-py3.11` to `-py3.14`. `docker compose up` stops without it. |
| `LAKANA_RUNNER` | `docker` (each Python task in a container of its own, isolated) or `local` (as the worker's own processes, not isolated); sets `LAKANA_RUN__RUNNER` on every worker. A bundle whose `.env` predates the key runs `docker`. With `local`, `launcher` and `egress` still start and need what they need (see [Python assets](../how-to/deploy-with-compose.md#python-assets)). |
| `LAKANA_DOCKER_SOCKET` | The Docker socket the `launcher` service mounts (`/var/run/docker.sock`). |
| `LAKANA_EGRESS_UPSTREAM_PROXY` | `http://[user:password@]host:port` of the proxy your network sends https through, for environment builds; empty for none. |
| `LAKANA_EGRESS_REFUSED` | Address ranges no build, task or connection test may reach, besides the ones always refused: comma-separated CIDR ranges or addresses (`192.168.1.0/24,10.0.0.7`), at most 64; empty for none. Put this host's own LAN address or subnet here. Sets `LAKANA_LAUNCHER__EGRESS_REFUSED`; see [Addresses builds and tasks never reach](network-settings.md#addresses-builds-and-tasks-never-reach). A bundle whose `.env` predates the key refuses no extra range. |
| `LAKANA_INGESTION_ROOTS` | JSON object of root name → absolute host directory, such as `{"exports": "/srv/exports"}`; `{}` (none) by default. Each root is mounted read-only into each task with a `directory` or `sqlite-file` connection that names it; the launcher refuses system directories. Sets `LAKANA_LAUNCHER__INGESTION_ROOTS`; see [Files on this host](../how-to/deploy-with-compose.md#files-on-this-host). A bundle whose `.env` predates the key has no roots. |
| `LAKANA_BIND` | The host address the server's port is published on (see [Exposing it](../how-to/deploy-with-compose.md#exposing-it)). |
| `LAKANA_PORT` | The host port the server is published on. |
| `LAKANA_WORKERS` | Worker replica count, used by `deploy: {replicas: ${LAKANA_WORKERS}}`. |
| `LAKANA_WORKERS_INTERACTIVE` | Replicas of `worker-interactive`, workers that take ad-hoc queries only (`0`: none). A bundle whose `.env` predates the key starts none. See [Separate workers for ad-hoc queries and pipelines](../how-to/deploy-with-compose.md#separate-workers-for-ad-hoc-queries-and-pipelines). |
| `LAKANA_WORKERS_PIPELINE` | Replicas of `worker-pipeline`, workers that take pipeline work only: deploys, tasks, maintenance (`0`: none). A bundle whose `.env` predates the key starts none. |
| `LAKANA_WORKER_MEMORY` | The memory limit of each worker container, of all three kinds (`4g`). The worker stops itself at 90 percent of it. See [Memory limits](../how-to/deploy-with-compose.md#memory-limits). |
| `POSTGRES_PASSWORD` | The `lakana` Postgres user's password (also the catalog's connection string). |
| `LAKANA_S3_ACCESS_KEY` / `LAKANA_S3_SECRET_KEY` | SeaweedFS's S3-compatible credentials, used by both SeaweedFS's own identity config and the `setup` job that binds them to the `dev` environment. |
| `LAKANA_SECRETS__MASTER_KEY` | The master key that protects every stored secret: it wraps the key of each secret. Losing it makes every stored secret unrecoverable. |
| `LAKANA_BOOTSTRAP_PAT_KEY_ID` / `LAKANA_BOOTSTRAP_PAT_SECRET` | The bootstrap admin credential (`lakana_pat_<key id>_<secret>`), saved into the `team` profile so the CLI can authenticate immediately. |

## The services

| Service | Image | Role | Starts after |
|---|---|---|---|
| `postgres` | `postgres:16` | Catalog metadata. | — |
| `seaweedfs-core` | `chrislusf/seaweedfs:4.47` | SeaweedFS's master, volume server and filer, holding the `seaweed` volume. Only on the internal `seaweed-internal` network (see [Known gaps of the Compose bundle](../explanation/compose-known-gaps.md)). | — |
| `seaweedfs` | `chrislusf/seaweedfs:4.47` | The S3 gateway, the only way to the object storage: `http://seaweedfs:8333`. | `seaweedfs-core` healthy |
| `bucket` | `chrislusf/seaweedfs:4.47` | One-shot: creates the `lakana` bucket if it doesn't already exist. | `seaweedfs` healthy |
| `migrate` | `${LAKANA_IMAGE}` | One-shot: applies Alembic migrations (`lakana-server migrate`) and exits. | `postgres` healthy |
| `server` | `${LAKANA_IMAGE}` | The FastAPI control plane and Iceberg REST catalog; publishes `${LAKANA_BIND}:${LAKANA_PORT}:8000`. | `migrate` completed |
| `launcher` | `${LAKANA_IMAGE}` | The only holder of the Docker socket: starts each Python task and each environment build in a container of its own, on the worker's request over a Unix socket. No network, no capabilities, a read-only filesystem (see [`launcher` and `egress`](../how-to/deploy-with-compose.md#launcher-and-egress)). | — |
| `egress` | `${LAKANA_IMAGE}` | The only way out of environment builds and of Python tasks with connections: a proxy that tunnels to the package index hosts the launcher signed for each build, and to the endpoints of the HTTPS connections a task's asset declares. A task with database or `tcp` connections also gets a relay of its own, a container the launcher starts from this service's image and removes with the task (see [Connections: what a task reaches](../how-to/run-python-tasks-in-containers.md#connections-what-a-task-reaches)). A task's `directory` and `sqlite-file` connections read the roots of `LAKANA_INGESTION_ROOTS`, mounted read-only. | `launcher` healthy |
| `worker` | `${LAKANA_IMAGE}` | The DuckDB query executor and dequeue loop; scaled to `LAKANA_WORKERS` replicas; takes every kind of work. | `migrate` completed, `launcher` healthy |
| `worker-interactive` | `${LAKANA_IMAGE}` | The same worker with `LAKANA_WORKER__TAKES=interactive`, scaled to `LAKANA_WORKERS_INTERACTIVE` replicas (`0` by default, so no container starts). | `migrate` completed, `launcher` healthy |
| `worker-pipeline` | `${LAKANA_IMAGE}` | The same worker with `LAKANA_WORKER__TAKES=pipeline`, scaled to `LAKANA_WORKERS_PIPELINE` replicas (`0` by default). | `migrate` completed, `launcher` healthy |
| `setup` | `${LAKANA_IMAGE}` | One-shot: creates the `dev` environment, its storage secret, and the binding between them, entirely over the REST API (see [`setup`](../how-to/deploy-with-compose.md#setup)). | `server` healthy and `bucket` completed |

## Healthchecks

What `healthy` in `docker compose ps` means for each long-running service. The one-off services
(`bucket`, `migrate`, `setup`) have none; `Exited (0)` is their healthy state.

| Service | Its healthcheck | Healthy proves |
|---|---|---|
| `postgres` | `pg_isready` on its network address, every 5 s. | The database accepts connections. |
| `seaweedfs-core` | The master answers and the filer accepts connections, every 5 s. | The storage's internal services run. |
| `seaweedfs` | The S3 gateway answers HTTP, every 5 s. | The gateway listens; not that the credentials work. |
| `server` | `GET /healthz` from inside the container, every 10 s. | The server process answers HTTP. |
| `launcher` | A connection to its Unix socket, every 5 s; the launcher removes an earlier launcher's socket before it starts. | The launcher started: it checked the Docker Engine and listens for the workers. |
| `egress` | A TCP connection to port 3128, every 10 s. | The proxy listens; not that a build reaches an index or a task its sources. |
| `worker`, `worker-interactive`, `worker-pipeline` | `lakana-worker healthcheck`, every 10 s, after a start period of 30 s; unhealthy after 3 failures in a row. | The worker of the container wrote its heartbeat to the database within `LAKANA_RUN__LEASE_SECONDS` (60 s), so it reaches the database. |

The server's healthcheck asks `/healthz`, not `/readyz`, on purpose: `setup` waits for a healthy
server and the workers wait for `setup`, so a server that is healthy only once a worker is alive
would never start. To know whether the instance can do work (the database, the storage of each
environment and a live worker), ask `/readyz`:
[The health routes](../how-to/instance-health.md#the-health-routes).

## Volumes

| Volume | Mounted by | Holds | Kept small by |
|---|---|---|---|
| `pgdata` | `postgres` | The catalog database. | Nothing; it grows with tables, runs and audit events. |
| `seaweed` | `seaweedfs-core` | The object storage: every table, artifact and code archive. | Table maintenance and the sweep of dropped tables; `lakana env usage` shows its size per environment. |
| `worker-cache` | `worker`, `worker-interactive`, `worker-pipeline`, at `/home/lakana/.cache/lakana-worker` | With `LAKANA_RUNNER=local`: `venvs/` (Python environments) and `uv/` (uv's package download cache), shared by every worker. | `LAKANA_RUN__VENV_KEEP` environments; the sweep trims `uv/` above `LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES` (10 GiB). `lakana worker list` shows its size. |
| `task-work` (named `<project>-task-work`) | the workers, at `/home/lakana/.cache/lakana-worker/attempts`; the launcher, only so that it gets its owner | Task directories; the launcher mounts each task's code and scratch directory from here. | The sweep removes leftover task directories. |
| `launcher-socket` | `launcher`, the workers, at `/run/lakana-launcher` | The launcher's Unix socket and the token the workers send. | Nothing to keep small. |
| `launcher-state` | `launcher` | One marker per built environment. | Kept with the environments. |
| `egress-key` | `launcher` (writes), `egress` (reads), at `/run/lakana-egress` | The key that signs each build's and task's egress, a new one at every launcher start, and `refused.json`, the address ranges the proxy refuses. | Nothing to keep small. |
| `<project>-env-…`, launcher's caches | task and build containers, made by the launcher and labelled `lakana.launcher.stack=<project>` | One built environment each, and the build caches. `docker compose down -v` does not remove them: see [Starting over](../how-to/deploy-with-compose.md#starting-over). | The launcher keeps the 10 most recently used environments. |

See [Disk use](../how-to/instance-health.md#disk-use).

## Secrets settings

| Variable | Default | Meaning |
|---|---|---|
| `LAKANA_SECRETS__MASTER_KEY` | none | Generated by `init` (see the table above). |
| `LAKANA_SECRETS__ACCEPT_FORMAT_1` | `false` | `true` or `false`. Whether secrets stored in the old format, written by a release before the stored format was bound to its row, are read. Leave it `false`. Turn it on only while you set old secrets again after an upgrade, on the server and on every worker, and remove it afterwards: [Manage secrets](../how-to/manage-secrets.md#secrets-stored-in-the-old-format) and [Upgrade to the release with artifacts](../how-to/upgrade-to-artifacts.md#secrets-stored-before-this-release). |

How to turn the second one on: [Secrets settings](../how-to/deploy-with-compose.md#secrets-settings).

## Operator settings

The variables an operator changes most, set under `x-lakana-env` in `compose.yaml` (see
[Settings](../how-to/deploy-with-compose.md#settings) for how to apply a change):

| Variable | Default | Meaning |
|---|---|---|
| `LAKANA_ARTIFACTS__MAX_FILE_BYTES` | `2147483648` (2 GiB) | Largest single file of an artifact version (above 0). |
| `LAKANA_ARTIFACTS__MAX_BYTES` | `10737418240` (10 GiB) | Largest total size of one version (above 0). |
| `LAKANA_ARTIFACTS__MAX_FILES` | `10000` | Most files in one version (above 0). |
| `LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS` | `3600` | How long an upload may stay open, and how long its commit can be repeated (above 0). The server and the worker must read the same value, and their clocks must agree far more closely than this value. |
| `LAKANA_TABLE_READS__KEEP_DAYS` | `90` | How many days of reads of each table are kept (1 or more); `lakana table health` and the health route accept a window up to this. The worker that holds the sweep role deletes older counts every `LAKANA_RUN__SWEEP_INTERVAL`. The server and the worker both read it. Details: [Table health](../reference/table-health.md#how-long-reads-are-kept). |
| `LAKANA_TABLE_READS__DEFAULT_DAYS` | `30` | The window the readers of a table cover when a request names none (1 or more, and not above `LAKANA_TABLE_READS__KEEP_DAYS`; the server stops at start otherwise, naming both). |
| `LAKANA_APPEND__MAX_CONCURRENT` | `4` | Files (and table appends) the server receives at once, per server process. More is answered 503, and the SDK asks again. |
| `LAKANA_APPEND__READ_TIMEOUT_SECONDS` | `300` | An upload that sends no byte for this long is refused with 408. |
| `LAKANA_REQUEST_BODY__MAX_BYTES` | `1048576` (1 MiB) | Largest body of an ordinary (JSON) request, above 0. A larger one is answered 413 before the server decodes it or checks the caller: a declared length over the limit at once, any other body as soon as the bytes received pass it. Table data and artifact files are not counted: they have the limits above. |
| `LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS` | `100000` | Longest SQL text accepted: a submitted query or write, a row-filter or column-mask expression, and a SQL asset at deploy (above 0). A longer one is refused with a message that names this setting. The server and the worker both read it. |
| `LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS` | `900` | Longest one query may run (above 0); for a write, the time to store the result counts. At the limit the worker asks the query to stop, and the query fails with the cause `timeout` and a message that names this setting. A person can end a query sooner: [Stop a query](../how-to/stop-a-query.md). |
| `LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS` | `10` | Seconds between asking a query to stop (its time limit, a cancel, a worker that shuts down) and ending its process (0 or more, a finite number). A query that had to be ended after its time limit or a cancel request says so in its message. |
| `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` | `auto` | The most ad-hoc queries one user or service account runs at once, counted over every environment. A further query waits in the queue and is not refused; it starts when fewer than the limit of that person's queries are running, and other people's queries are not held back meanwhile. Pipeline work (deploys, tasks and the queries they run, maintenance) is never counted. With `auto`, one user may run ad-hoc queries on every worker that takes interactive work while there are one or two such workers, and on all of them but one from three on. A whole number of 1 or more replaces the rule: `1` with two workers keeps one free for others, and a number at or above the number of workers means no limit. `auto`, in any letter case, or a whole number; anything else (0, a negative or fractional number, other text) stops the process at start with a message that names this setting. A query running on a worker that was lost still counts until the scheduler ends it, about a minute. The server and every worker read it, so set it in the shared `x-lakana-env` block and nowhere else. |
| `LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE` | `30` | `AUTHORIZATION_DENIED` rows the audit log records per principal per minute, in each server and each worker (above 0). A refusal past it is still refused, but not recorded: the next row recorded for that principal has `suppressed` in its detail, the number not recorded. The first refusal of each action is always recorded. Every refusal that is not recorded is still written to the log of that server or worker (`docker compose logs`), one INFO line from `lakana_core.audit.authorization` that starts `AUTHORIZATION_DENIED not recorded in the audit table` and names the principal, the action and the object; filter on it if the log grows. So the audit table stays bounded, and which object was probed after the limit is reached is in the log, not in the table. When a server or worker stops cleanly, the counts not yet recorded are written as one summary row per principal, for at most 100 principals and about 3 seconds so that a slow database cannot delay the stop; a warning says how many principals' counts were left out. |
| `LAKANA_LOG__LEVEL` | `INFO` | How much the server and the worker log: `DEBUG`, `INFO`, `WARNING` or `ERROR`, in any letter case; anything else stops the process at start with a message that names the setting. Lakana's own lines and the server's start-up and error lines follow it, and uvicorn's access log (one line per request) is printed at `INFO` and `DEBUG` only. The progress of a migration (alembic's "Running upgrade" lines, from `lakana-server migrate`) follows the level too. Other libraries log from `WARNING` at every level. Above `INFO`, the denials over `LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE` leave no line in the log, only the summary row in the audit table. Set it for the server and the worker alike (the `x-lakana-env` block does). |
| `LAKANA_LOG__ACCESS` | `true` | Whether the server prints uvicorn's access log, one line per request. `false` turns off that line and nothing else. `lakana server start --local` sets it to `false` unless you pass `--log-requests`. Only the server reads it. |
| `LAKANA_RUN__MAINTENANCE_TIMEOUT` | `1800` | Seconds one table maintenance run (`lakana table maintain`) may take, 30 minutes by default; more than 0. The worker checks the time between its steps and between the files it deletes, stops at the first safe point and fails the run with the cause `timeout`; the table stays consistent, and running maintenance on it again continues where it stopped. A Python task's input query waits behind a maintenance run on the same worker for about this long: the run stops at the first safe point after it. Only the workers read it. |
| `LAKANA_RUN__SWEEP_INTERVAL` | `3600` | Seconds between the artifact, table-location and code-archive sweeps of the worker that holds the scheduler role, and between the attempt-directory and deploy-checkout sweeps of every worker. `0` turns them off. |
| `LAKANA_TABLES__ABANDONED_GRACE_HOURS` | `24` | Hours a create of a managed table that did not finish, and that no pipeline run made, keeps its files before the sweep removes them (1 to 8760); a create a run made keeps them until the run has ended. The sweep never removes a table that exists. Only the workers read it. Details: [The sweep](../how-to/deploy-with-compose.md#the-sweep). |
| `LAKANA_TABLES__UNDO_DAYS` | `7` | Days a dropped managed table keeps its files and can be restored with `lakana table undrop` (0 to 365); after that the sweep deletes them, at the latest one sweep later. `0` deletes them at the next sweep and allows no restore. Each drop records its own end of the period. The server and the workers read it (a worker for the tables it drops because their retention has passed, which keep at least a day), so set it in the shared `x-lakana-env` block. Details: [Drop and restore a table](../how-to/drop-and-restore-a-table.md). |
| `LAKANA_TABLES__EXPIRY_WARNING_DAYS` | `7` | Days ahead `lakana table list` and `lakana table get` say that a table's retention lets it expire, and the default window of `lakana table expiring` (0 to 3650; `0` turns the warning off). Only the server reads it. Details: [Let tables expire](../how-to/let-tables-expire.md). |
| `LAKANA_TABLES__MAX_EXPIRIES_PER_SWEEP` | `100` | The most tables one sweep drops because their retention has passed (1 to 10000), the most overdue first; the rest follow at the next sweep. Only the workers read it. |
| `LAKANA_WORKER__TAKES` | `all` | What a worker takes: `all`, `interactive` or `pipeline`, in any letter case. Anything else stops the worker at start with `LAKANA_WORKER__TAKES must be all, interactive or pipeline (got 'X').` Only the worker reads it, so it does not belong in the shared `x-lakana-env` block: set it in the environment of the workers it is meant for. What the values mean, and when to use them: [Separate workers for ad-hoc queries and pipelines](../how-to/deploy-with-compose.md#separate-workers-for-ad-hoc-queries-and-pipelines). |
| `LAKANA_WORKER__NOTIFICATIONS` | `true` | Whether this worker delivers job notifications (webhooks). `false` stops it from sending any; another worker must deliver, or messages wait as `queued`. A worker with no usable `LAKANA_SECRETS__MASTER_KEY` delivers none either and logs a warning at start. Only the worker reads it. What a message holds and how to test it: [Get told when a job fails](../how-to/get-told-when-a-job-fails.md). |
| `LAKANA_RUN__CA_BUNDLE` | none | Path, inside the worker, of a PEM file of the private certificate authority of a package index or of a source your Python tasks read. Builds and tasks of both runners trust it in addition to Mozilla's roots; it is mounted read-only into each build and task container. Mount the file into the `worker` service read-only and set this on the worker only. A file with a private key, without a certificate or over 1 MiB stops the worker. Details: [The trust bundle of builds and tasks](../reference/network-settings.md#the-trust-bundle-of-builds-and-tasks). |
| `LAKANA_WEBHOOK__ALLOW_HOSTS`, `LAKANA_WEBHOOK__PROXY`, `LAKANA_WEBHOOK__CA_BUNDLE` | none | Where a worker may send a webhook: host names that may resolve to a private address (fully qualified, names whose DNS you control only; never loopback or a metadata address), a proxy to tunnel through (deny private, loopback, link-local and `100.64.0.0/10` destinations in its access rules), and a CA file that replaces trust. Set them on the worker only. Meaning and limits: [Webhook network settings](../reference/network-settings.md#webhook-network-settings). |
