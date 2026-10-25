# Deploying with Compose (team mode)

`lakana bundle init` generates a Docker Compose bundle that runs a complete Lakana instance —
Postgres for the catalog, SeaweedFS for object storage, the server, one or more workers, and
a one-shot setup job — on a single machine or VM, for a small team sharing one instance.
This is the "team" deployment path; for a single user on their own laptop, see local mode
(`lakana server start --local`, `packages/lakana/README.md`).

## Prerequisites

- **Docker Engine 28 or later** with **Compose ≥ 2.24** (`docker compose version`). The bundle
  runs each Python task in a container of its own, which needs Engine 28; the `launcher` service
  stops at start with a message on an older one.
- `pip install lakana` — the `lakana` CLI generates the bundle and is also the client you use
  against it afterwards. Lakana is pre-beta: the API and the data layout may change between 0.x
  releases.
- The image `ghcr.io/lakana-labs/lakana` is public; pulling it needs no login.

## Verify a download

Every release after 0.13.0 carries a signed build provenance attestation, made by Lakana's
release workflow, for each wheel, each sdist and the image. It proves that the file or image
you hold was built by the `lakana-labs/lakana` repository from the tagged commit. Checking it is
optional. You need the [GitHub CLI](https://cli.github.com/) (`gh`). Download a wheel and verify
it, or verify the image you pulled:

<!-- not tested: needs a published release and the GitHub CLI -->
```bash
pip download "lakana==<version>" --no-deps
gh attestation verify lakana-<version>-py3-none-any.whl --repo lakana-labs/lakana \
  --signer-workflow lakana-labs/lakana/.github/workflows/release.yml
gh attestation verify oci://ghcr.io/lakana-labs/lakana:<version> --repo lakana-labs/lakana \
  --signer-workflow lakana-labs/lakana/.github/workflows/release.yml
```

`--signer-workflow` ties the check to Lakana's release workflow, not to any workflow of the
repository. A passing check prints the repository and the workflow that built the file. A failing check
means the file is not the one Lakana published: delete it and download again. A release up to 0.13.0
has no attestation, and the command says that no attestation was found.

## Quickstart

<!-- not tested: starts a Docker Compose stack, and the example runner has no Docker -->
```bash
lakana bundle init
cd lakana-deploy && docker compose up -d --wait
lakana sql "SELECT 42"
```

`lakana bundle init` writes `compose.yaml`, `.env` and an empty `repos/` (for local git
repositories, see below) into `./lakana-deploy` (override with a positional `DIRECTORY`
argument), generates fresh secrets, and saves a profile named `team`
(see "Profile name" below) so the `lakana` CLI can talk to the new instance right away.
`docker compose up -d --wait` blocks until every one-shot service (`migrate`, `bucket`,
`setup`) has actually finished, not just started, and fails if any of them exits non-zero —
`worker` depends on `setup` completing successfully, the same way `server`/`worker` already
depend on `migrate`, so `--wait` itself is the gate; there's no second command to run after
it, and none should be added (a separate `docker compose wait setup` step here used to race
`setup`'s own exit on a restart, where it sends two quick `GET`s and exits in under a
second).

## Local repositories

`lakana bundle init` creates `repos/` next to `compose.yaml`; the server and the workers see it
read-only as `/repos`, and `LAKANA_REPO_ROOTS` is `["/repos"]`. Clone or `lakana init` a project
there and register it with `lakana repo add NAME --kind local --url /repos/NAME`. Only committed
state is deployed. Remote repositories (`https`, `ssh`) need no mount.

## What `init` generates

`compose.yaml` (see [The services](../reference/compose-bundle.md#the-services)), an empty `repos/` directory (mode `0755`) that
`server` and `worker` mount read-only as `/repos` with `LAKANA_REPO_ROOTS='["/repos"]'`, and
`.env` (mode `0600`, since it holds every secret this deployment has):

The keys of `.env`, with their purpose, are in
[The `.env` file](../reference/compose-bundle.md#the-env-file).

**Why `init` refuses to overwrite an existing bundle:** re-running it into a directory that
already has a `compose.yaml` or `.env` would generate a *new* `LAKANA_SECRETS__MASTER_KEY`,
silently locking you out of every secret already encrypted under the old one. `init` checks
both files (a dangling symlink counts as existing) before writing anything, and refuses
(exit 2) rather than risk that. If a run fails partway through — writing `.env` or
`compose.yaml`, or saving the profile afterwards — it removes whatever it already created,
including the directory if it created that too, so a retry starts clean instead of tripping
this same refusal over debris from a run that never produced a working deployment. A
filesystem error unrelated to a pre-existing bundle (the directory can't be created or
written to) exits 3 instead, naming the path.

**Profile name.** The saved profile's name comes from the CLI's global `--profile` option
(default `team`), not from a `bundle init`-specific flag — `lakana --profile NAME bundle init`
names it `NAME` instead. `init` refuses (exit 2) if a profile of that name already exists,
checked *after* the bundle-existence check above, so whichever conflict is actually relevant
to the target directory is the one you see.

**`--image`.** A caller-supplied `--image` is validated against the characters safe in
`.env` and a Compose file (letters, digits, and `._/:@-`) — anything else, including quotes,
`$`, `#`, or whitespace, is refused (exit 2) before it can corrupt either generated file. A
development or local build of the CLI (a version `hatch-vcs` derives as a dev snapshot, not a
tagged release) has no matching published image, so `--image` is required for it; a release
build defaults to `ghcr.io/lakana-labs/lakana:<its own version>`.

**`--task-image`.** The task images, one per Python (`<reference>-py3.11` to `-py3.14`), default
to the Lakana image's repository with `-task` and the same tag:
`ghcr.io/lakana-labs/lakana:0.20.0` gives `ghcr.io/lakana-labs/lakana-task:0.20.0`, written to
`.env` as `LAKANA_TASK_IMAGE`. An `--image` without a tag (pinned by digest) has no such
default, and `init` asks for `--task-image` (exit 2). Give it without `-py3.X`, ending in a tag
and in lower case; anything else is refused with the reason.

### Secrets settings

The two variables, with their defaults, are in
[Secrets settings](../reference/compose-bundle.md#secrets-settings): `LAKANA_SECRETS__MASTER_KEY` (generated by `init`) and
`LAKANA_SECRETS__ACCEPT_FORMAT_1` (`false`).

The bundle needs the setting for none of its own secrets: its `setup` service sets the storage secret
`seaweedfs-s3` again at the next start if it is still in the old format. You act only for secrets you
created yourself.

To turn it on, add the line to the shared block of `compose.yaml` (the `server` and the `worker` both
read it) and run the `docker compose` command of the [Quickstart](#quickstart) again:

```yaml
x-lakana-env: &lakana-env
  # ... the lines that are already here stay ...
  LAKANA_SECRETS__ACCEPT_FORMAT_1: "true"
```

## The services

Each service, its image, its role and what it starts after, is in
[The services](../reference/compose-bundle.md#the-services).
The server and every worker connect to `postgres` directly, and should keep doing so if you point
them at another PostgreSQL. Each connection asks PostgreSQL to probe the machine behind it, so
that a worker that lost power or its network is noticed (see "Scaling workers"). Behind a
connection pooler the probes test the pooler, not the worker, and a pooler in transaction mode
also breaks the scheduler's role itself, which is a session lock. The request does nothing on a
unix socket (harmless: both ends are on one machine), and on a PostgreSQL host that is not Linux
only the keepalive settings decide, because `tcp_user_timeout` has no effect there. PostgreSQL
before version 12 refuses `tcp_user_timeout`; the warning in the log of the worker or server then
names it.

### `launcher` and `egress`

The `launcher` service is the only one that holds the Docker socket. The worker asks it, over a
Unix socket in a volume only the two share, to start each Python task and each environment build
in a container of its own; it has no network, no capabilities and a read-only filesystem. `egress`
is the only way out of a build container: it lets a build reach the hosts of the package indexes
its project may use and nothing else. It is also the only way out of a task whose asset declares
connections: such a task reaches exactly those connections' endpoints (see
[Connections: what a task reaches](run-python-tasks-in-containers.md#connections-what-a-task-reaches)). If your network sends https through a proxy, set
`LAKANA_EGRESS_UPSTREAM_PROXY` in `.env`. Docker Engine 28 or later is required; the launcher
stops at start with a message on an older one. `LAKANA_DOCKER_SOCKET` in `.env` is the socket it
mounts (`/var/run/docker.sock`; Docker installed as a snap uses the same path).

### `setup`

`setup` (`python -m lakana.cli.deploy.setup`) runs once per `docker compose up`, using the
bootstrap admin credential and the SeaweedFS S3 keys from `.env`, and does everything through
the public REST API via the SDK — it has no direct database or storage access of its own
(rule 2: the REST API is the only way in). It retries up to 30 times, 2 s apart — a bit
under a minute of retrying in total — while the server is still settling (a fresh Postgres
and a fresh server container can both take a moment after their own healthchecks pass), then
fails the container if the server never comes up.

It is idempotent: a restart re-runs it and it creates nothing a second time. If `dev` already
exists with a *different* storage root, or its storage secret is already bound to some other
name, `setup` treats it as **operator-managed** and leaves it completely untouched — no
secret created, no rebind attempted — printing a line saying so instead. This is what lets an
operator repoint `dev` at their own storage after the first run without `setup` fighting them
on every subsequent `docker compose up`. `setup` looks after `dev` only; for another environment see
[Add an environment to a Compose instance](#add-an-environment-to-a-compose-instance). When `dev` is at the bundle's root and has no storage secret yet, `setup` sets its storage options to
the bundle's and attaches the secret in one request, and prints a line saying so, because the
options decide where the secret is sent. Options an operator set there are replaced.

### Python assets

Each Python task runs in a container of its own (`LAKANA_RUN__RUNNER=docker`, set by
`LAKANA_RUNNER` in `.env`): its code and environment read-only, a private scratch directory, the
server as the only thing it can reach besides the connections its asset declares, and its own memory (`LAKANA_RUN__TASK_MEMORY`, 2 GB), CPU
(1) and process (256) limits. Environments are built once per `uv.lock`, `pyproject.toml`, Python
and set of package indexes, in volumes the launcher names `<project>-env-…`. What that isolates
and what it does not, its settings and its messages are in
[Run Python tasks in containers](run-python-tasks-in-containers.md); what a project's Python code
can and cannot do is in [Python assets](projects.md#python-assets).

`LAKANA_RUNNER=local` changes only where tasks run: as the worker's own processes, without
isolation. `launcher` and `egress` still start, so Docker Engine 28, the Docker socket and
`LAKANA_TASK_IMAGE` are still needed (the launcher's start-up checks pull the 3.11 task image), and
the workers still wait for a healthy launcher. To run without them, remove both services and the
workers' `launcher` entry under `depends_on` from your `compose.yaml` by hand.

A task talks to the server over REST, so the worker needs the server's address: the bundle sets
`LAKANA_RUN__SERVER_URL=http://server:8000` on the `worker` service (not on the server). The
worker keeps its task directories in the `task-work` volume, named `<project>-task-work`, from
which the launcher mounts each task's code and scratch directory. With `LAKANA_RUNNER=local` the
image's `uv` builds one venv per `uv.lock` and `pyproject.toml` pair in the named volume
`worker-cache`, mounted at `/home/lakana/.cache/lakana-worker`, shared by all worker replicas and
kept across restarts. `docker compose down -v` removes both; the next task rebuilds what it needs.

The volume does not clean itself completely. The workers remove the directory a task leaves
behind when a worker is killed mid-task (`LAKANA_RUN__SWEEP_INTERVAL`, default every hour; the same
setting paces the artifact store's sweep, the sweep of code archives that failed deploys left and the
sweep of deploy checkouts, and `0` turns all off, so nothing collects attempt
directories, artifact leftovers or those) and keep the newest `LAKANA_RUN__VENV_KEEP` venvs. The same sweep
trims uv's own download cache (`uv/` in the volume) above `LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES`
(10 GiB by default), never removing a package downloaded within
`LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS` (24) and never while a worker builds an environment;
`lakana env usage` shows how large it is ([Disk use](instance-health.md#disk-use)). To reclaim all
of it at once, stop the workers and run
`docker compose run --rm --no-deps --entrypoint sh worker -c 'rm -rf /home/lakana/.cache/lakana-worker/uv'`.

The cache directory must be private to the worker: owned by uid 10001 and mode `0700`, or every
Python task fails with "not private". The image creates it that way and a new named volume
inherits that, so the bundle needs no setup. Its `attempts/` is the `task-work` volume, which
takes the same owner and mode from the launcher's mount, made before any worker starts; a worker
logs a warning, and fails Python tasks, if `attempts` is a link or not private. If you replace the volume with a bind mount or set
`LAKANA_RUN__VENV_CACHE` yourself, create the directory first with
`chown 10001:10001 DIR && chmod 0700 DIR`.

The environment build downloads packages. With the docker runner a build reaches the hosts the
project's `uv.lock` names (PyPI by default) through `egress` only, and `LAKANA_RUN__HTTPS_PROXY`
is refused (set `LAKANA_EGRESS_UPSTREAM_PROXY` in `.env` for a proxy). With the local
runner the worker needs outbound HTTPS to those hosts, or `LAKANA_RUN__INDEX_URL`, `LAKANA_RUN__HTTPS_PROXY`
and `LAKANA_RUN__NO_PROXY` set on the `worker` service by hand (same rules
as the `LAKANA_GIT__*` settings below; put a credential in the URL's `user:token@` part: a query string is refused). Without network access a Python task fails at its
environment build, and the task's log has uv's error. Whether a worker can run Python assets is
logged when it starts (`Python assets: enabled` or `disabled: <reason>. To enable it: <fix>`).

If your package index, or a source your tasks read, uses a certificate of a private certificate
authority, mount that authority's PEM file into the `worker` service read-only and point
`LAKANA_RUN__CA_BUNDLE` at it, with either runner. In `compose.yaml`, **add** one line to the
`worker` service's `environment:` and one to its `volumes:`, and keep every entry that is there:
the worker needs its cache, the launcher's socket and the task volume, and the docker runner
fails without them. `worker-interactive` and `worker-pipeline` take both lists from `worker`
(`<<: *worker`, `<<: *worker-env`), so they get the file too:

```yaml
x-lakana-env: &lakana-env    # unchanged: the bundle's shared settings
  LAKANA_DATABASE__URL: postgresql+pg8000://lakana:${POSTGRES_PASSWORD}@postgres:5432/lakana
  LAKANA_SECRETS__MASTER_KEY: ${LAKANA_SECRETS__MASTER_KEY}

services:
  worker: &worker
    # ... (image, command and the rest unchanged)
    environment: &worker-env
      <<: *lakana-env
      LAKANA_REPO_ROOTS: '["/repos"]'
      LAKANA_RUN__SERVER_URL: http://server:8000
      LAKANA_RUN__RUNNER: ${LAKANA_RUNNER:-docker}
      LAKANA_RUN__CA_BUNDLE: /certs/corp-ca.pem              # added
    volumes:
      - ./repos:/repos:ro
      - worker-cache:/home/lakana/.cache/lakana-worker
      - launcher-socket:/run/lakana-launcher
      - task-work:/home/lakana/.cache/lakana-worker/attempts
      - ./certs/corp-ca.pem:/certs/corp-ca.pem:ro            # added
```

Builds and tasks then trust Mozilla's roots plus that file; the worker mounts it into each build
and task container itself. A file with a private key, without a certificate or over 1 MiB stops
the worker with a message that names the cause:
[The trust bundle of builds and tasks](../reference/network-settings.md#the-trust-bundle-of-builds-and-tasks).

`LAKANA_TASK_TOKENS__MAX_TTL_SECONDS` caps the lifetime of every task token, and the server and the
worker both read it. If you change it, set it to the same value on the `server` and the `worker`
service: a cap lowered on the server only is not applied to the tokens the worker mints.

## Storage for artifacts

Artifacts (files such as trained models that Python assets store, see
[Artifacts](../reference/artifacts.md)) live in the environment's object storage, under
`artifacts/` in the storage root. That directory is reserved: do not put anything of your own
there, and never point two Lakana deployments at one storage root, because each would treat the
other's artifacts as leftovers.

**Bytes pass through the server.** The server streams a file to storage in pieces of 4 MiB and
never holds a whole file in memory, so its memory use does not depend on file size. Its network does:
every uploaded and downloaded file crosses the server. Plan the bandwidth between the workers,
the people and the server for the artifacts you store.

### Settings

Set these where the server and the worker read them. In the bundle, the lines under `x-lakana-env`
in `compose.yaml` reach the `migrate` job, the `server`, the `worker` and the two optional worker services
`worker-interactive` and `worker-pipeline` (see the example below). Every variable with its type,
default and reader is in the [settings reference](../reference/settings.md); the table below explains
the ones an operator changes most.

The variables an operator changes most, with their defaults and what each does, are in
[Operator settings](../reference/compose-bundle.md#operator-settings).

The clients learn the three size limits from the server, so people and tasks see the refusal before
they send a file. Add the settings you change to the shared block:

```yaml
x-lakana-env: &lakana-env
  # ... the lines that are already here stay ...
  LAKANA_ARTIFACTS__MAX_BYTES: "21474836480"
  LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS: "7200"
```

Then apply them by running the `docker compose` command of the [Quickstart](#quickstart) again, from
the bundle's directory.

### A reverse proxy in front of the server

A proxy must let these requests through, or users meet errors that name no Lakana setting. It
should also keep out what the server would refuse anyway, so that it never reaches the server:

- **Limit an ordinary request body to a few megabytes** on every path except the ones that
  stream files and table data (`.../tables/<name>/data` and `.../artifacts/...`). The server
  refuses a JSON body over `LAKANA_REQUEST_BODY__MAX_BYTES` (1 MiB) with 413, but only after the
  proxy has passed it on. A proxy that buffers a request in full takes that cost itself.
- **A request body as large as your largest file** (2 GiB by default). Many proxies refuse bodies
  of a megabyte or so unless you raise the limit.
- **No buffering of the request body** in memory or on disk, if the proxy can be told. A file is
  streamed, so buffering needs room for a whole file.
- **A long timeout for a drop.** `lakana artifact drop` removes files inside the request and can run
  for minutes (the client waits up to ten). A proxy that ends it sooner shows the user a 504 for a
  drop that still finishes.

### Interrupted uploads on an object store

If a server dies in the middle of a file, an S3-compatible store keeps the parts it received, and
a listing does not show them. Lakana cannot find or remove them. Set a rule on the bucket that
aborts incomplete multipart uploads. In S3 it is the lifecycle action `AbortIncompleteMultipartUpload`,
for example after one day:

```json
{
  "Rules": [
    {
      "ID": "abort-incomplete-uploads",
      "Status": "Enabled",
      "Filter": {"Prefix": ""},
      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}
    }
  ]
}
```

Apply it with your store's console or the S3 call `PutBucketLifecycleConfiguration`. Whether a
store supports the rule depends on the store. This guide has not checked it for the SeaweedFS of the
bundle; read its documentation, and if it does not, accept that interrupted uploads leave parts
that use space until you remove them with the store's own tools. A local `file://` storage root
does not have this problem: an interrupted write leaves a `.part` file, which the sweep removes.

### The sweep

Once an hour (`LAKANA_RUN__SWEEP_INTERVAL`) the worker that holds the scheduler role removes what
a crash or an abandoned upload left: expired uploads, files no version needs, half-written files
and the manifests of deleted versions. One sweep runs for at most five minutes and the next one
continues, so a large backlog goes down over several runs. To drain one faster, set a smaller
interval for a while. One limit: a run starts each artifact's pass over its file records from the
beginning, and its time per artifact is fixed, so an artifact with more than about 200,000
distinct files is not finished by later runs either (the sweep's summary line says so when a run
stops at its time limit). The sweep keeps what a restore or a failover may have made the catalog forget
(a directory without a catalog entry, a manifest object of an unknown version and the files it
names) and says so in the worker's log: see "After a restore" below. A file with no record at all is
removed after seven days, or at once when the manifest of a deleted version names it. A file that a
version or an upload in progress names is kept, whatever its record says.

A deleted version's files are removed in the request that deletes it, up to about 200 files or
five seconds, and the sweep removes the rest. A drop can leave the empty directories `blobs` and
`manifests` behind, because the storage interface cannot remove a directory.

**Tables whose create never finished.** Every managed table's directory under `managed/` has a
location record in the catalog, written before the first file of the table. When a create fails,
it removes its own files at once if it knows that nothing was committed. When it cannot know (the
database did not confirm the commit), it keeps them, and the same sweep decides later: once the
run that made the create has ended, or, for a create outside a run, once
`LAKANA_TABLES__ABANDONED_GRACE_HOURS` (24 by default) have passed, it checks under a lock whether
the table exists. If it does, everything is kept. If not, the sweep removes the files and then the
record. It never removes a directory that has no record, nor one that holds or lies around a table
that exists, in any environment that shares the storage root, nor one that is, holds or lies (at or under
`managed/`) around another environment's storage root, nor any in an environment whose root lies
in another environment's `raw/`, `artifacts/` or `_artifacts/` directory, nor one that the same place under another
name uses (a symbolic link, another case, a doubled slash, a `.` or `..` segment, a percent escape;
a relative or host `file` location keeps everything on the local disk; it logs `The table location
sweep left <location> alone: <location>, another environment's storage root or a location written
another way …`): it counts such directories and logs
`<n> table directories under managed/ in the storage of environment <env> have no location record;
they are left alone.` (see "After a restore" below). Directories that older releases left behind are
counted the same way; remove them on request with `lakana env orphaned-locations` (see
[Clean up files left by older releases](clean-up-files-left-by-older-releases.md)). The audit log records `TABLE_LOCATION_ABANDONED`
when the sweep gives up a create (detail: `location`, `holder_run_id`, `claimed_at`) and
`TABLE_PURGED` when it removes files (detail: `location`, `state`, `files`, `bytes`, and `complete`,
which is `false` for a pass that stopped part-way and is continued by the next sweep). Both have no
principal: the sweep does them.

**Dropped tables.** A drop keeps a managed table's files for the undo period
(`LAKANA_TABLES__UNDO_DAYS`, 7 days by default), during which `lakana table undrop` restores the
table. The same sweep deletes them afterwards: no earlier than the end of the undo period, and no
later than one sweep after it; a drop with `--purge` is due at once. Before it deletes the first
file it marks the record, so a restore after that is refused instead of bringing back a table with
files missing. A table whose directory holds a dropped table of its own (in a namespace named
after that directory) is purged only once the table inside has been restored or purged itself; until
then the worker logs `The table location sweep left <location> alone for now: it waits for a dropped
table inside it, …` at each sweep. An external table's files are never deleted. With versioning on the bucket, the
bucket's own rules decide when a deleted file is gone. See
[Drop and restore a table](drop-and-restore-a-table.md).

**Deleted environments.** A deleted environment's tables are dropped tables like any other, and the
sweep deletes their files after the undo period in the same way. Once none of its tables' files are
left and its own undo period has passed, the sweep removes the environment from the catalog too
(audit event `ENVIRONMENT_PURGED`, with no principal), and its name is free again. Nothing in a
deleted environment expires. See
[Delete and restore an environment](drop-and-restore-a-table.md#delete-and-restore-an-environment).

**Tables whose retention has passed.** Before anything else, the same sweep drops the managed
tables that their retention (`lakana table retention`, `lakana env retention`) lets expire, at most
`LAKANA_TABLES__MAX_EXPIRIES_PER_SWEEP` (100) per sweep, the most overdue first. Each is dropped like
a drop by a person, with an undo period of at least a day, and the audit log records
`TABLE_DROPPED` with `reason: retention` and no principal. It never drops a table in a protected
environment, one a deployed project builds, or one a write is running on (the summary line counts
those as `expiries postponed`). Nothing has a retention unless someone sets one. See
[Let tables expire](let-tables-expire.md).

**The worker's log.** The worker prints INFO and above to its standard error, with the time, the
level and the logger name on each line; read it with `docker compose logs worker`. That includes
the start-up notices (whether Python assets are enabled, and that the sweeps are off when
`LAKANA_RUN__SWEEP_INTERVAL=0`) and the sweep's summary line. `LAKANA_LOG__LEVEL` sets the level
(see "See more or less in the log" below).

**The server's log.** The server prints the same way: `LAKANA_LOG__LEVEL` (`INFO` by default) and above to its standard error, with
the time, the level and the logger name on each line (`docker compose logs server`), next to
uvicorn's own lines. The INFO lines of Lakana itself are few: the line for a refused request that
the audit table does not record because its principal is over
`LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE` (it starts `AUTHORIZATION_DENIED not recorded in the
audit table`, from `lakana_core.audit.authorization`), and a note that the removal of a deleted
artifact's files stopped at its time budget and the sweep does the rest. Filter on the logger name
to find them. Earlier versions printed only warnings from the server's own code, so those lines
were lost.

**Control characters in the log.** Both processes write control characters (an escape, a carriage
return as `\x0d`, a bell, the bidirectional marks, a lone surrogate) as text such as `\x1b`, in
every line, a library's and an exception's text included, uvicorn's access lines too, so a name or
a path that someone chose cannot recolour a terminal or hide text; a tab becomes four spaces. Where
a record has several lines (an error with its traceback), every line after the first starts with
four spaces and `| `, so a line that does not start with a space is always the start of a record
(the server's own start with a time, uvicorn's with `INFO:     ` and the like), never a line made
up inside another. A record longer than 1 MiB of text is cut and ends in `… (N more characters
left out)`. The colours of uvicorn's level names are off for the same reason.

**See more or less in the log.** Both processes read one setting, `LAKANA_LOG__LEVEL`: `INFO` by
default, `WARNING` or `ERROR` for less, `DEBUG` for more. Put it in the `x-lakana-env` block of
`compose.yaml` and run the `docker compose` command of the [Quickstart](#quickstart) again:

```yaml
x-lakana-env: &lakana-env
  # ... the lines that are already here stay ...
  LAKANA_LOG__LEVEL: "WARNING"
```

At `WARNING` and `ERROR` the server also stops printing one line per request, and above `INFO`
the denials over the audit rate leave no line in the log, only the summary row in the audit table:
keep `INFO` if you read that log to see what was probed. At `ERROR` the start-up warning about
grants that have no effect (see [Upgrade policy](../explanation/upgrades.md#delegation-needs-the-admin-role))
is not shown either; `lakana grant list` marks those grants in every case. The progress of a migration (each "Running upgrade A -> B" line while
`lakana-server migrate` runs) follows the level, so you see it at `INFO` and `DEBUG`. `DEBUG` adds
nothing else from libraries (an HTTP client's or a SQL driver's own lines stay off), so it stays
readable. A level that is not one
of the four stops the process at start and says so.

## Exposing it

By default `--bind 127.0.0.1` keeps the server reachable only from this machine. To expose
it on the network, pass a specific address or `--bind 0.0.0.0` for every interface —
**either way, the server speaks plain HTTP**, and `init` warns on stderr for any non-loopback
bind, wildcard included:

<!-- output -->
```
Warning: --bind 0.0.0.0 is not loopback; the server accepts plain HTTP connections from your network.
```

Put TLS in front of it with a reverse proxy instead of binding it to the network directly.

**A proxy that handles TLS by itself, in one block:**

<!-- not tested: configuration of a reverse proxy (Caddy), which is not Lakana's and not parsed here -->
```
lakana.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

**A proxy with explicit limits:**

<!-- not tested: configuration of a reverse proxy (nginx), which is not Lakana's and not parsed here -->
```
server {
    server_name lakana.example.com;
    client_max_body_size 300m;       # sized for table appends
    proxy_read_timeout 300s;         # long-running queries
    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

Then pass `--public-url https://lakana.example.com` to `lakana bundle init` so the saved profile
(and the URL `init` prints for operators) points at the proxy, not the bind address. A
non-loopback `--public-url` with an `http://` scheme gets the same plain-HTTP warning.

### `--bind` and the profile URL

`--bind` accepts an IP address (IPv4 or IPv6) or the literal `localhost`. The profile URL
`init` saves (and prints, absent `--public-url`) depends on how it classifies the bind:

- **Loopback** (`127.0.0.1`, `::1`, `localhost`) or **wildcard** (`0.0.0.0`, `::`) — the
  profile URL is `http://127.0.0.1:<port>`, since that's how this same machine reaches the
  server either way.
- **A specific, non-loopback address** — the profile URL uses that address, e.g.
  `http://203.0.113.5:8000`; an IPv6 address is bracketed (`http://[2001:db8::1]:8000`).

`localhost` is written into `.env`'s `LAKANA_BIND` as `127.0.0.1` — Compose's `host_ip` in the
port mapping must be an IP literal, not the word `localhost`.

## Scaling workers

<!-- not tested: scales the services of a running Docker Compose stack, and the example runner has no Docker -->
```bash
docker compose up -d --scale worker=4
```

or edit `LAKANA_WORKERS` in `.env` and re-run `docker compose up -d`.

With the default `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL=auto`, a third worker that takes
interactive work switches on a reserve: one user then runs ad-hoc queries on two of the three
workers, so that others are not kept waiting. With one or two such workers there is no reserve, and
only a fixed `1` gives one; see the [settings](#settings). A query that waits because of the limit
says `user_limit`, with the number running and the limit; see
[Why is my query or task waiting?](why-is-it-waiting.md#a-queued-query).

`lakana worker list` (it needs `MANAGE_ENVIRONMENT` on the instance, as `lakana scheduler status`
does) shows the workers that registered:

<!-- source: docs/examples/getting-started/run.sh#worker-list -->
```bash
lakana worker list
```

<!-- output -->
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━┳━━━━━━━┓
┃ id                                  ┃ last_seen ┃ status ┃ takes ┃ doing ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━╇━━━━━━━┩
│ analytics-box.local:237049:5d654503 │ 9s ago    │ alive  │ all   │ idle  │
└─────────────────────────────────────┴───────────┴────────┴───────┴───────┘
Not shown: started_at, version, host. Use --output json to see them.
```

`status` is worked out by the server, with the database's clock: `alive` while the worker's last
heartbeat is younger than `LAKANA_RUN__LEASE_SECONDS`, `lost` after that (the worker stopped, or
cannot reach the database). `last_seen` is the age of that heartbeat, shown by your machine's
clock, so a wrong local clock can show an odd age beside a correct `status`. The row of a lost
worker stays for an hour, so that you can see which one it was, and then disappears; the scheduler
has ended the queries and deploys it held long before (see below). `takes` is the work the
worker takes: `all`, unless `LAKANA_WORKER__TAKES` is set to `interactive` or `pipeline` (see
[Separate workers for ad-hoc queries and
pipelines](#separate-workers-for-ad-hoc-queries-and-pipelines)). `doing` is `idle`, or
`query ID`, `deploy ID` or `task ID` for the work the worker carries (the first 8 characters of
the run's id; a worker that supervises a Python task can also run a query, which then reads
`query 3f2c9d1e, task 7a41b0c2`). `--output json` always has the host, the version, the start
time and the whole ids; the table leaves the host, the version and the start time out when the
terminal is narrow and says so under it. In a terminal of 80 columns a long worker id wraps onto
a second line; from 100 columns on a row is one line. No worker listed means none has
registered: start one (`docker compose up -d`, or `lakana server start --local` on a
local instance).

Each worker also takes part in electing the run scheduler; stopping one
(`docker compose stop worker`) lets a running task finish for up to 20 s
(`LAKANA_RUN__DRAIN_GRACE_SECONDS`) before handing it to another worker, which is why the bundle
sets `stop_grace_period: 45s`. A Python asset's process then gets another 10 s
(`LAKANA_RUN__KILL_GRACE`) to exit and 2 s to drain its output, so releasing a Python task takes
about 32 s with the defaults; 45 s leaves margin. If you raise either setting, raise
`stop_grace_period` with it. A worker killed first loses nothing but time: the scheduler reclaims
the task when its lease expires.

The server and every worker must read the same `LAKANA_RUN__LEASE_SECONDS` (60) and
`LAKANA_RUN__RENEW_SECONDS` (20). If you change either, change it in the bundle's shared
`x-lakana-env` block, which the server and every worker use. Every worker
writes a heartbeat to the database every renew interval, and a worker counts as alive while its
heartbeat is younger than the lease. Keep the lease at least twice the renew interval; a worker
warns at start-up when it is not, because one late heartbeat would then make it count as lost.

A worker that is killed, loses power or has not written a heartbeat for the lease (a database that
is away for the lease minus the renew interval, 40 s with the defaults, can be enough) no
longer counts as alive, and the scheduler ends the query runs and the deploys it held within about a
lease (a minute with the defaults) and one scheduler step, and up to `LAKANA_RUN__RENEW_SECONDS`
plus 5 s more after the scheduler was itself stalled or has just taken over. That bound holds when another worker can take over the scheduler's role. Every connection to
PostgreSQL asks it to probe the client and drop the connection after about two thirds of
`LAKANA_RUN__LEASE_SECONDS` once its machine no longer answers, so the role of a worker that lost
power or its network on another machine than the database's is expected to be freed within about a
lease. If PostgreSQL refuses these
settings, the worker or server logs a warning once at the start and keeps running; then PostgreSQL may keep that connection for hours, and until it closes
nothing ends lost queries or reclaims tasks. `lakana scheduler status` shows which worker holds the role and when it last reported; if that worker is gone and the role stays with it for much longer than a minute, ending its database session in PostgreSQL (`pg_terminate_backend`), or restarting PostgreSQL, frees it; restarting the other workers does not. A read or a write that had not been committed
fails with the cause `worker_lost` and a message that says to run it again; a write that had
already been committed ends `SUCCEEDED`, so a user is not told to repeat it; the same holds for
the write of a pipeline's SQL task, and the task ends `succeeded` and is not retried (a task whose
table has [checks](check-your-data.md) is taken again instead, and only checks). A cancel of such a
query answers at once. A deploy, dry run or rollback it held ends `failed` with a message that
says nothing was activated and to deploy again, and a deploy that was only frozen and finishes
later changes nothing (see "Deploy, dry-run, roll back" in [Projects and deploys](projects.md)).
The reason the server must read the same lease as the workers: it decides
by that lease which workers are alive when someone cancels a query, so a server with a shorter
lease than the workers would cancel at once a query that a healthy worker is still running. The
details for the person who runs the query are in [Stop a query](stop-a-query.md).

Each query runs in a process of its own, which the worker starts and can end. That costs about a
tenth of a second per query (measured: 80 ms for `SELECT 42` on a 20-core machine), and it is why a
query past its time limit always ends. A query submitted on its own that is running when you stop a worker gets the same
`LAKANA_RUN__DRAIN_GRACE_SECONDS`, is then asked to stop and, `LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS`
later, ended; its run fails with the message "The query was stopped because the worker is shutting
down. Run it again." That includes the queries that a Python task sends through the API to load its
inputs: they are ordinary queries. The SQL task of a pipeline is released instead: after the grace
it runs again on another worker. Raise `stop_grace_period` with either setting. The query process gets the file list of every
table the query reads (about half a kilobyte per data file) and refuses a request over 64 MiB before
it starts; the message names the number of files and says to
write the table again with `replace` so that its data lands in fewer, larger files.

### Separate workers for ad-hoc queries and pipelines

Every worker takes every kind of work by default (`LAKANA_WORKER__TAKES=all`), and that is right
until the two kinds get in each other's way. The setting divides the work in two:

- **Interactive work:** the ad-hoc queries, writes and views that a person or a tool submits.
- **Pipeline work:** deploys, the tasks of pipelines and the queries they run (the inputs a Python
  task reads, a SQL task's own write), and table maintenance, also the maintenance a person starts
  by hand.

A worker with `LAKANA_WORKER__TAKES=interactive` takes interactive work only; one with
`pipeline` takes pipeline work only; `all` takes both. Start a second kind when people wait for
their ad-hoc queries while pipelines run, or when pipelines wait behind ad-hoc queries: run some
workers with `interactive` and the others with `pipeline`. Until then leave every worker on `all`.
Outside the Compose bundle, set the variable in the environment of each worker.

**In the Compose bundle** the two kinds are services of their own, `worker-interactive` and
`worker-pipeline`, defined like `worker` (same image, same memory limit, same restart policy) and
started with two numbers in `.env`. Both are `0` by default, so a bundle runs the workers it always
did, and the `worker` service keeps taking everything. For example, `LAKANA_WORKERS_INTERACTIVE=2`
and `LAKANA_WORKERS_PIPELINE=1` in `.env` start two workers for ad-hoc queries and one for
pipelines next to the `LAKANA_WORKERS` you have. Then run `docker compose up -d`. A count is a replica count; set a count back to `0` and run the
same command to stop that kind. **Lower a count to `0` with `docker compose up -d` and without
`--wait`:** with `--wait`, Compose 2.x (2.24 to at least 2.38) stops the containers and then fails
with `is missing dependency <service>`. Nothing is wrong with the bundle then; run
`docker compose up -d --wait` once more and it passes. To move a whole installation over, lower `LAKANA_WORKERS` as you
raise the other two: the default `worker` service may go to `0`, nothing in the bundle needs it,
but the instance then needs a worker for each kind it uses (see below). Each added worker may use
the memory limit `LAKANA_WORKER_MEMORY` sets for every worker (`4g` by default), so an operator who
adds two workers adds two limits to what the machine must have; see "Memory limits". Check the
result with `lakana worker list`: its `takes` column shows `all`, `interactive` or `pipeline` for
each worker that registered. A bundle generated by an earlier release has neither service; see
"Upgrading".

- **The scheduler role does not depend on it.** Every worker takes part in electing the scheduler,
  whatever it takes, and the worker that holds the role also ends the runs of lost workers of both
  kinds.
- **`lakana worker list` shows it** in the `takes` column, and a worker logs at start what it
  takes.
- **A Python task keeps its queries.** A pipeline worker that supervises a Python task serves the
  task's own input queries itself, so a task never waits for an interactive worker (it can wait
  behind a maintenance run that was queued earlier and is served by the same worker, for about `LAKANA_RUN__MAINTENANCE_TIMEOUT`, 30 minutes by default). It leaves
  ad-hoc queries to the interactive workers.

The one misconfiguration to avoid: only `interactive` workers on an instance that deploys or runs
pipelines. Nothing fails; deploys, tasks and maintenance wait until a worker that takes pipeline
work starts, and a deploy does not say why. Keep one worker or more on `all` or `pipeline`: in the bundle, `LAKANA_WORKERS` at 1 or more, or
`LAKANA_WORKERS_PIPELINE` at 1 or more. The same holds the other way round:
with only `pipeline` workers, ad-hoc queries wait until a worker that takes interactive work
starts.

### Seeing that a kind has no worker

Look at the `takes` column of `lakana worker list`: at least one alive worker must take each kind
you use. When it does not, the people who wait are told:

- **A query, a maintenance run and a task of a pipeline** show the reason `no_worker_of_kind`,
  for example `No worker that takes interactive work is running (2 workers take pipeline work
  only), so this query cannot start. Ask the operator to start a worker with
  LAKANA_WORKER__TAKES=interactive or all.` Start a worker with that setting.
- **A deploy** that stays `queued` while only `interactive` workers run is the sign to look for:
  start a worker that takes `pipeline` or `all`.

[Why is my query or task waiting?](why-is-it-waiting.md) lists the sentences and what each asks
of the person who reads it.

## Memory limits

A query can use more memory than DuckDB's own setting (`LAKANA_QUERY_LIMITS__MEMORY_LIMIT`, about
1 GB by default) says: some operations, and the code that turns a result into a table, use more.
Without a hard stop, one large query can fill the machine until the operating system kills the
worker, or the whole host stops responding. Each worker therefore has a memory limit, 4 GB by
default.

- **The container.** Each worker container gets a limit and no swap, from `LAKANA_WORKER_MEMORY`
  in `.env` (`4g`). If a worker goes beyond it, the kernel ends the worker, and Docker starts the container again
  (`restart: unless-stopped`). Use at least `1g`: a worker needs about 512 MB to start, and it stops
  itself at 90 percent of the container's limit.
- **The worker.** Inside the container the worker reads that limit and checks its own memory five times a
  second: the worker plus the tasks and queries it started (every query runs in a process of its
  own). Over the limit (90 percent of the container's), the worker measures each running query
  and each running Python task and **ends the largest one first**, at once, whichever kind it is.
  A query fails with the cause `out_of_memory` and the message below. A Python task fails that
  attempt with the second message below and is retried while it has attempts left (`retries + 1`
  in all). The worker and what runs beside the ended one go on. The worker then measures
  again (the ended process's memory is gone within a moment). Only if it is still
  over the limit, or nothing it can end was running, does the worker stop itself, and Docker
  starts it again.
  When the worker has to stop itself, whatever it was running ends with it, and is marked failed
  (a query, or the query run of a task, with the cause `out_of_memory`), so you see why instead
  of waiting for a lease to expire, when memory grows slowly enough for the worker to notice; a
  very fast query can still reach the container's limit first. A task has no cause field of its
  own; the cause is on its query run. A task is retried if it has attempts left (`retries + 1` in
  all), so one bad query cannot stop workers for ever. A query is not retried.
- **Task containers.** A Python task's container has its own limit, `LAKANA_RUN__TASK_MEMORY`
  (2 GB by default), outside the worker's. A task over it ends with "this task was ended because
  it used more than its container's memory limit" and is retried while it has attempts left.
  Budget the host for the workers' limits plus one task's limit per worker.

To change it, edit one line in `.env` and run `docker compose up -d`: set `LAKANA_WORKER_MEMORY`
to, for example, `8g`. A bundle made by an older version has no such line and uses 4 GB. The limit
is per replica, and `worker-interactive` and `worker-pipeline` use the same value:
`LAKANA_WORKERS`, `LAKANA_WORKERS_INTERACTIVE` and `LAKANA_WORKERS_PIPELINE` added together, times
`LAKANA_WORKER_MEMORY`, must fit the machine, with room left for Postgres, SeaweedFS and the
server. Two workers at `4g` need 8 GB for the workers alone, and two more of either kind need 8 GB
more.

When the worker ends a query it logs `Memory limit exceeded: the worker and its tasks use <n> MB,
the limit is 3686 MB, 90% of the container's 4096 MB; raise the container's memory limit (in the
Compose bundle: LAKANA_WORKER_MEMORY in .env). The largest consumer, a query (process <pid>), was
ended; the worker keeps running if that frees enough memory. event=memory_task_ended` and that
query fails with the cause
`out_of_memory` and the message `Query failed: this query made the worker exceed its memory limit
(3686 MB) and was stopped. Read or compute less (filter or aggregate earlier, or select fewer
columns), or ask the operator to raise the worker's memory limit (LAKANA_WORKER__MEMORY_LIMIT, or
LAKANA_WORKER_MEMORY in the Compose bundle's .env).`

When it ends a Python task, the line reads `... The largest consumer, a Python task (asset
<asset>, task run <id>, process <pid>), was ended; ...`, and the task's attempt fails with `this
task was ended because the worker exceeded its memory limit (3686 MB) and the task was the
largest consumer. Use less data in the task (narrow its inputs with an
upstream SQL asset), or ask the operator to raise the limit (LAKANA_WORKER__MEMORY_LIMIT, or
LAKANA_WORKER_MEMORY in the Compose bundle's .env).` The task is retried like any transient
failure, so a task that always needs more than the limit is ended again on every attempt until its
attempts run out; narrow its inputs or raise the limit. A task whose virtual environment is still
being built cannot be ended (the build cannot be interrupted); if the build is what grew and
nothing else can be ended, the worker stops itself.

When the worker stops itself it logs `Memory limit exceeded: the worker and its tasks use <n> MB,
the limit is 3686 MB, 90% of the container's 4096 MB; raise the container's memory limit (in the
Compose bundle: LAKANA_WORKER_MEMORY in .env). The worker stops now. What it was running is marked failed; a task is retried if it
has attempts left. event=memory_stop` (the codes: [Log events](../reference/log-events.md)). A
Python task with checks that may have written its table does not end over
unchecked rows when the stop ends it: on its last attempt, or on any attempt when the blame is
certain, the next worker sets the table back first (see [A worker that is
lost](check-your-data.md#a-worker-that-is-lost)). A query that was running then fails with the cause `out_of_memory` and the message
`Query failed: the worker ran out of memory and was stopped (its limit is 3686 MB). Read or compute
less (filter or aggregate earlier, or select fewer columns), or ask the operator to raise the
worker's memory limit (LAKANA_WORKER__MEMORY_LIMIT, or LAKANA_WORKER_MEMORY in the Compose
bundle's .env).` A query that fails on DuckDB's own limit says `LAKANA_QUERY_LIMITS__MEMORY_LIMIT`
instead. A write that had already been committed when the worker stopped ends `SUCCEEDED`, because the
data is in the table and running it again would write it twice. A sort or aggregation over that limit fails the same way (`the query ran out of
memory and cannot spill to disk`, with the same advice): the bundle does not let DuckDB write
spill files to disk. What to do:

1. Narrow the query: filter or aggregate earlier, select fewer columns.
2. If the data is simply that large, raise `LAKANA_WORKER_MEMORY` in `.env`, or add memory to the
   machine.

If the kernel killed the container before the worker could stop itself, a task shows `lease
expired: the worker stopped renewing it. If it was stopped for using too much memory, see the
worker's memory limit in the documentation (LAKANA_WORKER__MEMORY_LIMIT, or LAKANA_WORKER_MEMORY in
the Compose bundle's .env).` `docker inspect` shows `OOMKilled` for a
container that Docker stopped for memory.

You can set `LAKANA_WORKER__MEMORY_LIMIT` in the worker's environment yourself (`4GB`, `512MB`,
`off`; at least 512 MB). It then replaces the 90 percent rule. Its sizes are binary, so `GB` and
`g` mean 1024 MB; DuckDB's `LAKANA_QUERY_LIMITS__MEMORY_LIMIT` is different: `GB` there is decimal
(`1GB` is 953.6 MiB) and `GiB` binary. A value DuckDB cannot read, such as `lots`, stops the
worker at start-up with `LAKANA_QUERY_LIMITS__MEMORY_LIMIT is not a size DuckDB accepts`; the same
holds for `LAKANA_QUERY_LIMITS__MAX_TEMP_DIRECTORY_SIZE`. Write a number and a unit, such as `1GB`
or `512MiB`.

Workers must not share a temp directory across containers: a starting worker removes the spill
directories whose owning process it cannot see.

Not covered:

- The worker's own memory (maintenance, a result being stored) cannot be ended apart from the
  worker. The worker ends the largest query or task that it can end, whatever its share. If the
  worker's own memory sits just under the limit, a small task or query beside it is ended, the
  total drops, and the worker goes on; each retry and each new query is then ended the same way,
  and the task ends `FAILED` with "the task was the largest consumer". Give the worker more
  memory (`LAKANA_WORKER_MEMORY` in `.env`); if its own memory keeps growing, restart the worker.
- The total counts every process in the worker's sessions, but a task's or query's size is its
  tree of child processes: a process a task detached by a double fork counts in the total and not
  in the task's size.
- A task that always needs more than the limit is ended and retried until its attempts run out.
  Give the worker more memory (`LAKANA_WORKER_MEMORY` in `.env`), or give the task less data
  (narrow its inputs with an upstream SQL asset).
- A task in its environment build phase (`uv sync`) cannot be ended; it is skipped when the
  worker chooses, and the worker stops if nothing else can be ended.
- A burst between two checks is not seen.
- On macOS the worker measures its own peak, which never falls, and always stops without ending a
  query or a task first.
- A deploy (git and the project's files) is not tied to a run, so it fails when its worker stops,
  without the message above.

## Running more than one bundle on a host

`compose.yaml`'s top-level `name: lakana` gives every bundle the same Compose project name.
That's fine for the single bundle this guide assumes, but two consequences follow directly
from it:

- **A second `bundle init` bundle on the same Docker host collides with the first.** Compose
  identifies a project's containers and volumes by project name, so `docker compose up` in
  the second bundle's directory takes over the first bundle's containers, and a later
  `docker compose down -v` there deletes the first bundle's `pgdata`/`seaweed` volumes.
- **The fix is `COMPOSE_PROJECT_NAME`, set before any `docker compose` command** — an
  environment variable takes precedence over `compose.yaml`'s own `name:`. Either
  `export COMPOSE_PROJECT_NAME=lakana-otherteam` once per shell, or add
  `COMPOSE_PROJECT_NAME=lakana-otherteam` to the bundle's own `.env` file, which Compose reads
  automatically from the bundle directory for exactly this purpose (as well as for its own
  `${VAR}` interpolation). Do this before the very first `up` for the second bundle —
  changing it afterwards makes Compose treat the existing containers as belonging to a
  different, new project.

## Add an environment to a Compose instance

`bundle init` creates one environment, `dev`, and gives it the bundle's storage and the secret
that holds the storage keys. A second environment (here `prod`) needs the same three things: the
storage root and endpoint, a secret with the keys, and the secret attached to the environment. A
secret belongs to one environment, so `prod` gets its own secret with the same keys under the
same name, `seaweedfs-s3`.

You need these rights, each at the scope shown: step 1 needs `MANAGE_ENVIRONMENT` on the instance;
step 2 needs `MANAGE_SECRET` on `prod`; step 3 needs `MANAGE_ENVIRONMENT` on the instance (not
only on `prod`) and, since 0.15.0, `MANAGE_SECRET` on `prod`. The bootstrap credential and
anyone with the `ADMIN` role on the instance have them all; the `ADMIN` role on `prod` alone does
not cover steps 1 and 3. Run the commands in the bundle's directory, with the
profile `bundle init` saved (see [Quickstart](#quickstart)).

1. Create the environment in the bundle's bucket, `lakana`, under its own prefix, with the
   endpoint the services use inside the Compose network. It has no secret yet. Write the root
   plainly, as below: a root spelled another way (`s3a://`, a doubled slash, a `.` or `..`
   segment, a percent escape) is refused with the spelling to use instead. A root inside another
   environment's `managed/`, `raw/`, `artifacts/` or `_artifacts/` (such as
   `s3://lakana/dev/raw/prod`) is refused too, with a root to use instead.

<!-- source: docs/examples/compose-add-environment.sh#create -->
```bash
lakana env create prod --storage-root s3://lakana/prod --option s3.endpoint=http://seaweedfs:8333
```

2. Create the secret from the storage keys in `.env` and bind it to its environment
   (`--own-env`). The keys go to the command on its standard input, so they appear in no
   shell history and no process list.

<!-- source: docs/examples/compose-add-environment.sh#secret -->
```bash
s3_key=$(grep '^LAKANA_S3_ACCESS_KEY=' .env | cut -d"'" -f2)
s3_secret=$(grep '^LAKANA_S3_SECRET_KEY=' .env | cut -d"'" -f2)
printf '{"s3.access-key-id": "%s", "s3.secret-access-key": "%s"}' "$s3_key" "$s3_secret" \
  | lakana secret create seaweedfs-s3 --env prod --own-env --values-stdin
unset s3_key s3_secret
```

3. Attach the secret to the environment. Pass the options again: attaching a secret approves the
   options the environment has then, because `s3.endpoint` decides where the secret is sent.

<!-- source: docs/examples/compose-add-environment.sh#attach -->
```bash
lakana env set-storage prod --option s3.endpoint=http://seaweedfs:8333 --secret-ref seaweedfs-s3
```

4. Check that it works: `env get` names the secret, and creating and dropping a table writes to
   and removes from the storage with the secret's keys.

<!-- source: docs/examples/compose-add-environment.sh#check -->
```bash
lakana env get prod
lakana table create bronze.check --env prod --column id:long
lakana table delete bronze.check --env prod --yes
```

`env get` shows `storage_secret_ref` as `seaweedfs-s3`. If the table cannot be created, the error
names the cause: a refusal says which right is missing (ask an administrator for it), and a
storage error means the keys or the endpoint are wrong (set the secret again with
`lakana secret rotate seaweedfs-s3 --env prod --values-stdin`, and see
[Manage secrets](manage-secrets.md)).

The bundle has one pair of storage keys, so `prod` is kept apart from `dev` by its tables and
its grants ([Give a second person access](give-a-second-person-access.md)), not by separate
storage credentials. To give an environment storage of its own, with keys of its own, create it
against that storage and bind a secret as [Manage secrets](manage-secrets.md) shows.

## Upgrading

What every upgrade asks, and what a release may break, is in the
[Upgrade policy](../explanation/upgrades.md).

Upgrading to the release with artifacts needs a database migration, the server and every worker on
the new release, and every project with Python assets locked again for it: see
[Upgrade to the release with artifacts](upgrade-to-artifacts.md) for the order and the way back.

Two things to check before editing `.env`:

1. **Pull the new tag first, before editing anything.** Pulling first fails loudly if the tag
   does not exist, rather than mid-upgrade with the old container already stopped.

   <!-- not tested: pulls the image of a release from the registry; `<new version>` is the reader's own -->
   ```bash
   docker pull ghcr.io/lakana-labs/lakana:<new version>
   ```

2. **`compose.yaml` is a static file `init` wrote once, with the CLI version that ran it —
   upgrading only the image tag does not pick up a `compose.yaml` change a newer `init`
   would generate** (a new service, a changed healthcheck, and so on). Compare your bundle's
   copy against a fresh one before assuming a tag bump is the whole upgrade:

   <!-- not tested: needs Docker and a published image; `<new version>` is the reader's own -->
   ```bash
   lakana --profile upgrade-check bundle init /tmp/lakana-upgrade-check --image ghcr.io/lakana-labs/lakana:<new version>
   diff lakana-deploy/compose.yaml /tmp/lakana-upgrade-check/compose.yaml
   rm -rf /tmp/lakana-upgrade-check
   lakana profile remove upgrade-check --yes
   ```

   `--profile upgrade-check` is required here: `init` refuses (exit 2) if a profile of the
   target name already exists, and the operator running this already has a `team` profile
   by definition — a bare `lakana bundle init /tmp/lakana-upgrade-check` would just fail with
   "Profile 'team' already exists". Only compare `compose.yaml` —
   `/tmp/lakana-upgrade-check/.env` holds unrelated, freshly generated secrets, not anything
   to merge in. Apply any structural `compose.yaml` difference to your own bundle by hand;
   `init` never writes into a directory that already has a bundle. The final `lakana profile
   remove` cleans up the scratch profile this check saved.

3. **A bundle made before the release with task containers** needs four `.env` lines and the new
   `compose.yaml` (compare as above):

   <!-- not tested: four .env lines; test_deploy_init.py checks that init writes them -->
   ```text
   LAKANA_TASK_IMAGE=ghcr.io/lakana-labs/lakana-task:<new version>
   LAKANA_RUNNER=docker
   LAKANA_DOCKER_SOCKET=/var/run/docker.sock
   LAKANA_EGRESS_UPSTREAM_PROXY=
   ```

   Without `LAKANA_TASK_IMAGE`, `docker compose up` stops with "set LAKANA_TASK_IMAGE in .env,
   see the upgrade notes". `LAKANA_RUNNER=local` keeps tasks in the worker's own processes, but
   the launcher still starts and needs all four lines and Docker Engine 28
   ([Python assets](#python-assets)).

### Limits that start to apply after you upgrade

Three bounds are new. Each has a default that no ordinary use reaches, a message that names its
setting, and a setting to raise it; check that none of your clients is above one before you upgrade.

- **A JSON request body over 1 MiB is answered 413.** The answer says "The request body is larger than
  1048576 bytes, the most the server accepts for a JSON request" and names
  `LAKANA_REQUEST_BODY__MAX_BYTES`, a setting of the server. Table data and artifact files are not
  counted. A client sends less in one request, or you raise the setting.
- **SQL text over 100,000 characters is refused.** That is a submitted query or write, the expression of
  a row filter or a column mask, and a SQL asset of a project: it is refused when it is submitted, when
  the policy is created and at the next deploy. Assets that are already deployed keep running. The
  message says "... characters long; the most Lakana accepts is 100000" and names
  `LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`, which the server and the worker both read: raise it on
  both.
- **At most 30 `AUTHORIZATION_DENIED` audit rows are written per principal per minute**, in each server
  process and each worker (`LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE`). A refusal past that is still
  refused, and is written to the log of that server or worker instead of the audit table, as an INFO line
  that starts `AUTHORIZATION_DENIED not recorded in the audit table`. If you read the audit table to
  find out who tried what, read that log as well once a principal is over the rate.

**The logs of the server and the worker have no lines of libraries at INFO.** The log level setting (`LAKANA_LOG__LEVEL`) keeps libraries at `WARNING`, so INFO lines such as the S3 client's note about where it found its credentials are gone, in the server and the worker alike; Lakana's own lines and the migration progress are as before.

**The server's log has more lines than before.** The server now prints the INFO lines of Lakana's own
code to its standard error, as the worker does (see "The server's log" above). Under an attack that
makes many refused requests, the log grows by one line for each refusal past the audit rate, where
the table does not; if a log collector bills or rotates by size, check its limits before you upgrade.

**One user's running queries are limited from three workers on.** The default of
`LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` is `auto`. With three or more workers that take ad-hoc
queries, one user or service account can have at most all but one of them busy at once (with three workers
that take ad-hoc queries, two queries running at once; with four, three), and a further query waits with the reason `user_limit` while a worker is free. With one or
two workers nothing changes, which includes the bundle's default of two. A script or a BI tool that
runs queries in parallel under one account is such a user. To keep the earlier behaviour, set the
variable to the number of your workers; give the server and every worker the same value.

A package index or proxy URL whose path has a segment that looks like a token (a cloud project id, for
example) is not refused; the server and the worker log a warning at start-up that names the setting.
It is there so that a credential in a path is not forgotten: only a URL's userinfo is redacted from logs.

**Bundles generated before the worker kinds** have the `worker` service only, and the two counts in
`.env` do nothing. Generate a new `compose.yaml` with `lakana bundle init` into an empty directory
and copy its `worker-interactive` and `worker-pipeline` services next to your `worker` (they merge
the definition of `worker`, so give it the anchors `&worker` and, on its `environment`,
`&worker-env` as the new file does), then add `LAKANA_WORKERS_INTERACTIVE` and
`LAKANA_WORKERS_PIPELINE` to `.env`.

**Bundles generated before local repositories were supported** have no `repos/` mount, so the server and workers refuse
every local repository ("LAKANA_REPO_ROOTS is empty"); remote (`https`, `ssh`) repositories work
without it. To use local repositories, create `repos/` next to `compose.yaml` and add both of
these to the `server` and the `worker` service by hand (the block shows the `server` in the context
of the file, so that the `*lakana-env` alias resolves; the `worker` gets the same two lines):

```yaml
x-lakana-env: &lakana-env
  # ... the lines that are already here stay ...
  LAKANA_DATABASE__URL: postgresql+pg8000://lakana:${POSTGRES_PASSWORD}@postgres:5432/lakana

services:
  server:
    # ... the lines that are already here stay ...
    environment:
      <<: *lakana-env
      LAKANA_REPO_ROOTS: '["/repos"]'
    volumes: ["./repos:/repos:ro"]
```

In such a bundle the `worker` has the bare alias `environment: *lakana-env`; change that line to
`environment: {<<: *lakana-env, LAKANA_REPO_ROOTS: '["/repos"]'}` (a bare alias cannot take a
second key).

**Bundles generated before Python assets were supported** cannot run Python assets: add these to the `worker` service
(an older `stop_grace_period` of 30 s should become `45s` at the same time) and add
`worker-cache:`
under the top-level `volumes:`:

```yaml
    stop_grace_period: 45s
    environment:
      LAKANA_RUN__SERVER_URL: http://server:8000
    volumes:
      - worker-cache:/home/lakana/.cache/lakana-worker
```

Then edit `LAKANA_IMAGE` in `.env` to the new tag and:

<!-- not tested: pulls an image from the registry and restarts a running Docker Compose stack -->
```bash
docker compose pull && docker compose up -d --wait
```

`migrate` runs first (its `depends_on: {postgres: {condition: service_healthy}}`, and
`server`/`worker` both depend on it completing successfully), so the schema is always
migrated before the new server or worker code runs against it.

## Backups

The catalog (Postgres) and the object storage are one state, so a backup is only consistent if
nothing writes to either while you take it. Stop what writes, dump the catalog while Postgres
still runs, stop the storage and archive its volume, then start everything again. The stack is
unavailable for the time this takes.

1. Stop the server and every worker service, so nothing writes any more.
2. Dump the catalog; this needs `postgres` to be running.
3. Stop the two storage services and archive the `seaweed` volume. Compose mounts the volume
   into a one-off container of the `seaweedfs-core` service that runs `tar`, so it finds the right
   volume whatever the project name is, whether you set `COMPOSE_PROJECT_NAME` in `.env`, in the
   shell, or not at all (see
   [Running more than one bundle on a host](#running-more-than-one-bundle-on-a-host)).
4. Start everything again, and keep a copy of `.env` and `compose.yaml` with the two backups: `.env`
   holds the master key.

<!-- source: docs/examples/compose-restore.sh#backup -->
```bash
docker compose stop server worker worker-interactive worker-pipeline
docker compose exec -T postgres pg_dump -U lakana lakana > lakana.sql
docker compose stop seaweedfs seaweedfs-core
docker compose run --rm --no-deps -T -v "$PWD":/backup --entrypoint tar seaweedfs-core czf /backup/seaweed.tgz -C /data .
docker compose up -d --wait
```

A backup taken while the stack runs is not consistent, whatever the order: a file removed
between the two (a dropped artifact, or a deleted version after the sweep) is still listed by the
restored catalog, and a version stored between them is missing from it. Take backups with the steps
above.

## Restore

A restore puts the catalog and the object storage back from one backup, taken as under
"Backups". The project's `compose-smoke` job runs the backup and these steps: it backs up, changes
a table, loses the instance, restores into a new directory, and compares the table, the runs and
an artifact.

**Where to restore.** Into the directory of the bundle you lost, if it is still there, or into a
new, empty directory (on a new machine, say). The directory needs the `compose.yaml` and the
`.env` of the same bundle: `.env` holds the master key, which decrypts the stored secrets, and the
storage keys, which the restored storage was set up with. A bundle made by a new `bundle init` has
other keys and cannot read the restored instance. So keep copies of `.env`, `compose.yaml`, the
`repos/` directory (local repositories live there) and the two backup files together, and
restore from them. Use the image version of the backup, or a newer one: `migrate` brings the catalog up to date when
you start it. Never restore a catalog into an older image than the one that wrote it.

1. In the directory, put the four files, and make sure `repos/` exists.

<!-- source: docs/examples/compose-restore.sh#restore-directory -->
```bash
mkdir -p repos
```

2. Remove the volumes of the instance you are replacing. This **deletes** its catalog and its
   data, so be sure the backups are good first. In a new directory on a new machine there is
   nothing to remove, and it does nothing.

<!-- source: docs/examples/compose-restore.sh#restore-clear -->
```bash
docker compose down -v
```

3. Start Postgres alone and load the dump. `ON_ERROR_STOP` and `--single-transaction` make a
   failed load leave the database empty rather than half full.

<!-- source: docs/examples/compose-restore.sh#restore-catalog -->
```bash
docker compose up -d --wait postgres
docker compose exec -T postgres psql -U lakana -d lakana -v ON_ERROR_STOP=1 --single-transaction < lakana.sql
```

4. Unpack the archive into the storage volume. Compose creates the volume, with its own labels
   and the project name from `.env`, and mounts it into a one-off container that runs `tar`.

<!-- source: docs/examples/compose-restore.sh#restore-storage -->
```bash
docker compose run --rm --no-deps -T -v "$PWD":/backup --entrypoint tar seaweedfs-core xzf /backup/seaweed.tgz -C /data
```

5. Before you start everything, decide about the sweep (below), then start it.

<!-- source: docs/examples/compose-restore.sh#restore-start -->
```bash
docker compose up -d --wait
```

6. Check the result. A table you know reads the rows it had at the backup (not the ones
   written after it), and the runs of the backup are listed. `bronze.demo` and `dev` are examples;
   use your own.

<!-- source: docs/examples/compose-restore.sh#check -->
```bash
lakana sql "SELECT id, name FROM bronze.demo ORDER BY id"
lakana runs list --env dev
```

Everything written after the backup is gone: rows, runs, grants and tokens. People whose token
was issued after it must get a new one. Secrets are back as they were, because they are in the
catalog, encrypted with the master key in `.env`.

**The sweep.** Once an hour the sweep removes what a crash or an abandoned upload left behind. After a restore the catalog and the storage can
disagree if they were not taken at the same moment, and the sweep is built to remove little then:
it keeps an artifact directory the catalog does not know and the files a manifest it does not know
names, and logs that it did. To keep it from removing anything at all while you check, set
`LAKANA_RUN__SWEEP_INTERVAL: "0"` in the shared `x-lakana-env` block of `compose.yaml` before step
5 (the workers read it there, not from `.env`), and remove the line once you have checked:

```yaml
x-lakana-env: &lakana-env
  # ... the lines that are already here stay ...
  LAKANA_RUN__SWEEP_INTERVAL: "0"
```

What each log line means is in [After a restore](#after-a-restore). There is no command yet to
bring an artifact back into the catalog or to make it forget one on purpose; issue
[#68](https://github.com/lakana-labs/lakana/issues/68) tracks it.

## After a restore

Restore the catalog (Postgres) and the object storage from backups of the **same moment**, as
under "Backups". Artifacts, and tables that were being created, are where a mismatch shows: storage may hold artifact files and
manifests that the restored catalog has never heard of, or the catalog may list versions whose files
are gone. This section assumes that the catalog came from a consistent backup, a database dump or
point-in-time recovery.

**Before you restore, if you are unsure:** set `LAKANA_RUN__SWEEP_INTERVAL: "0"` in the shared `x-lakana-env` block of `compose.yaml` (the workers read it there, not from `.env`; apply it as under [Settings](#settings)) so that
no sweep runs while the two disagree, and set it back when you have checked what follows. The same
holds after a failover that lost the last commits, and when you point a fresh database at an
existing bucket.

**What the sweep does when the catalog and the storage disagree.** It removes little, and it
says so in the worker's log when it keeps something back.

- A directory under `artifacts/` that has no catalog entry and no drop marker is left alone.
- A manifest object of a version the catalog does not know is never removed. The files it names
  are kept as long as it exists.
- A file that has no record in the catalog is removed once it is older than seven days, or at once
  when the manifest of a deleted version names it. A manifest object that the catalog does not know
  protects the files it names from both. So does a version that the catalog lists as not deleted,
  and so does an upload in progress.

So a file the catalog has forgotten is kept while a manifest object the catalog does not know
names it, while a listed version or an upload in progress names it, and while its directory has no
catalog entry and no marker. A file with no record, in a directory the catalog knows, that none of
these names, can go. If your bucket keeps old object versions, a file that
Lakana removed stays in the bucket until the bucket's own rule removes it.

**Tables and their storage locations.** The catalog keeps a record of every managed table's
directory, and the sweep removes a table's files only when its record says that a create never
finished. So a restored catalog cannot make the sweep remove the files of a table it lists, and it
never removes a directory it has no record of:

- A table created after the backup has no record in the restored catalog. Its directory under
  `managed/` is left alone and counted in the line `<n> table directories under managed/ in the
  storage of environment <env> have no location record; they are left alone. ...` (logger
  `lakana_core.catalog.location_sweep`). Nothing reads those files. **Keep the directory**; there
  is no command yet to bring such a table back into the catalog. `lakana env orphaned-locations`
  lists such a directory like any leftover of an older release, so after a restore never run it
  with `--delete` and an `--older-than` shorter than the backup's age (see
  [Clean up files left by older releases](clean-up-files-left-by-older-releases.md)).
- A table that was **being created at the moment of the backup** has a record that says the create
  had not finished. The restored catalog shows its run as running, the scheduler ends it about a
  minute after the start, and the next sweep then removes the files. If that table was in fact
  finished after the backup and you need its data, copy its directory out of storage before you
  set `LAKANA_RUN__SWEEP_INTERVAL` back: it is a directory under `managed/` whose name is the
  table's name, a hyphen and 32 hexadecimal digits, and whose table `lakana table list` does not
  show.

**What the sweep keeps, and the log lines that say so**

The worker prints these lines to its standard error: `docker compose logs worker`. Each starts
with the time, the level and the logger name (`lakana_core.artifacts.sweep`). Only a sweep prints
them, so with `LAKANA_RUN__SWEEP_INTERVAL=0`, or before the first sweep has run, you will not see them.

1. `<n> artifact directories in environment <env> have no catalog entry and no drop marker; they are
   left alone. ...` Under `artifacts/` there are directories whose artifact the catalog does not
   have. The files are intact. A drop marks its directory `.dropped` before it deletes the row, so
   an unmarked directory is not a drop: it is an artifact that the restored catalog forgot (for
   example one created after the backup). Nothing was deleted. Nothing can read those files either.
   **Keep the directory.** Restore a newer catalog backup if you have one. Lakana has no command
   to bring such an artifact back yet. Deleting the directory by hand cannot be undone, and the
   catalog cannot tell you what is in it. Delete it only if you know from your own records that
   nobody needs any version stored there.
2. `The artifact sweep found manifest objects for version <n> of artifact '<name>' that the catalog
   does not know; the files they name are kept. ...` Storage holds `manifests/<n>.json` for a
   version that has no row in the catalog. Either a commit failed part-way (harmless), or the
   catalog lost a version. The files that manifest names are kept. The line comes again at every
   sweep until the object is removed or the catalog knows the version again. See "A manifest the catalog does
   not know" below. Without this line you can still find such objects: list
   `artifacts/<id>/manifests/` in storage; every `<n>.json` whose number `lakana --output json
   artifact versions list` does not show (it shows deleted versions too) is one.
3. `The artifact sweep left '<name>' whole for this run: manifest objects the catalog does not know
   could not all be read, or there are more than 20 of them; ...` The sweep checks at most 20 such
   objects of at most 16 MiB each. It removes nothing of that artifact until the cause is
   gone: remove the objects only where "A manifest the catalog does not know" says that removing is
   safe, or fix the unreadable one. If you are repairing versions whose files are gone, this line is
   **stop sign 2** in "Versions whose files are gone".
4. `The artifact sweep left '<name>' whole: a stored manifest of a live version or open upload is
   damaged. ...` Nobody can download that version. The log names the artifact, not the version: try
   a download of each of its versions, and the damaged one fails with `The stored manifest of
   version <n> of <name> is damaged; delete the version and run the asset again.` Delete it with
   `artifact versions delete`. The delete also removes the version's manifest object
   `manifests/<n>.json`, and the sweep then removes the files that no other version names. Copy
   that object and the files it names first if you want them.
5. `<n> files of artifact '<name>' are named by a version or an upload but have no record; kept.
   ...` A version the catalog lists, or an upload in progress, names files that have no record. A
   restore from a consistent backup cannot cause that, so look for what removed the records.
   Nothing was deleted.

**A manifest the catalog does not know.** Find the artifact's id: the `uuid` field in the
`--output json` output of the `artifact list` subcommand, without the hyphens, is the directory
name under `artifacts/` in the storage root. The object is
`artifacts/<id>/manifests/<n>.json`. A manifest object is a small JSON list of file paths, sizes
and hashes, and the SHA-256 of its bytes is the `manifest_sha256` that `artifact versions list`
shows for a version. Compute the SHA-256 of the object and compare it with the versions **of the
same artifact**. The table of `artifact versions list` shows only the first 12 characters of the
digest; for the whole digest use `lakana --output json artifact versions list <name> --env <env>`.

- **It equals the `manifest_sha256` of a version that the list shows as not deleted: removing the
  object is safe.** That version has exactly the files the object names, and it keeps them. Only
  the record of version `<n>` goes, and a later version can get the number `<n>` again.
- **It equals only the `manifest_sha256` of a deleted version, or of none: keep the object,
  unless it names a file that a broken version is missing (next bullet).** A deleted version does not
  make it safe. A task can store a version with the same content as one deleted before, and that
  new version needs the files. If the catalog forgot such a version, the object may be the only
  thing that protects its files: if you remove it, the next sweep removes those files, and that
  cannot be undone. Ask whether a task or a person stored or read version `<n>` after your backup
  was taken (their messages say `Stored <env>/<name> version <n>`). If so, Lakana has no command
  to bring the version back yet ([issue 68](https://github.com/lakana-labs/lakana/issues/68)).
- **It names a file that a broken version is missing: remove it, unless somebody needs its
  version.** A broken version is one whose download fails with `is missing from storage` (see
  "Versions whose files are gone"). Look up each `sha256` of the object as
  `artifacts/<id>/blobs/<sha256>`: it names a missing file when one of them is absent. An object
  with the same SHA-256 as a broken version names the same files, so it names the missing one too.
  While it exists it does harm: it keeps the catalog's record that the missing file is stored, so
  deleting the broken version frees no record and running the asset again does not send the file.
  What the object still protects is only its other files. **Before you remove it, copy the object
  and the other files it names out of storage.** Keep the copy outside the storage root: put back at
  `artifacts/<id>/manifests/<n>.json` it would pose as the object of the next version that gets the
  number `<n>`, and the next run reuses that number. Removing the object lets the next sweep remove
  those other files that nothing else names: at once when the manifest of a deleted version names
  them, otherwise once they are older than seven days, which they may already be. If somebody
  needs version `<n>`, do not remove the object: that is **stop sign 1** in "Versions whose
  files are gone".
- **Remove any other such object only if you are certain that no task or person stored or read
  version `<n>`** (the restored catalog does not show runs after the backup; check the worker's and
  your CI's logs): it is then the leftover of a commit that failed. The next sweep may remove the
  files that no remaining version needs. When you are not certain, keep it. Keeping costs a log line
  at every sweep. While the object exists, a request that deletes a version of that artifact frees
  no record; the next sweep removes the records and files that no version and no such object
  names.

A commit never reuses a version number that storage has seen, so you will see **gaps in the
numbers**. That is normal after a restore, and after a commit that failed.

**Versions whose files are gone.** If you restore the catalog to a moment before you deleted a
version (or before a run with `keep_versions` pruned it), the catalog lists that version again, but
its files were removed. A download then fails with
`A file of version <n> of <name> is missing from storage, and the catalog still lists it as stored,
so running the asset again would not store it. Delete this version and every other version that has
the file (`lakana artifact versions delete`), then run the asset again.` **Running the asset again
does not repair this** when its output is the same, because the catalog still believes the file is
stored and sends nothing.

Two cases need no steps and lose nothing:

- **If you have a backup of the object storage from before the delete,** copy the missing files back
  from it into `artifacts/<id>/blobs/`. Each file keeps its name, which is its SHA-256 (never copy
  over a file that exists). The server does not check the bytes of a stored file, so download the
  version afterwards with `lakana artifact download`, which checks every file against the manifest:
  when that succeeds, the version is back and nothing was deleted.
- **If you restored to a moment before you dropped the artifact,** its row is back and all its files
  are gone, so every version is broken. `artifact drop` removes what is left, and the next run of
  the asset creates the artifact again as a new one (the grants given on the artifact itself are
  gone, see [Delete an artifact version](delete-an-artifact-version.md#drop-the-whole-artifact)).

Otherwise follow the steps below. Every step is one action with a sign that it worked. There are
three stop signs, below the steps: places where no documented action keeps everything. At a stop
sign, stop: do not delete or remove anything more of that artifact, keep what the sign names, and
read what it says.

**First keep the asset from running, until step 5.** A run that commits in between stores a new
version that is broken again. A run starts when a person or your CI starts it (`lakana run`, `lakana job run`) or
when a job's schedule fires: ask them to wait, pause the CI job, and pause the schedules
(`lakana job pause`; see [Run a job on a schedule](run-a-job-on-a-schedule.md)). `lakana runs
list --env <env>` shows the runs: wait until none of the asset is queued or running.

1. **Find the broken versions.** List the artifact's versions, as JSON:

   <!-- not tested: needs a restored instance; the names are placeholders -->
   ```bash
   lakana --output json artifact versions list <name> --env <env>
   ```

   Note the artifact's `uuid` (the `<id>` in storage is the `uuid` without hyphens) and every
   version whose `deleted_at` is `null`. Try a download of each of them into an empty directory
   (see [Inspect and download versions](inspect-and-download-artifact-versions.md)):

   <!-- not tested: needs a restored instance; the names are placeholders -->
   ```bash
   lakana artifact download <name> <empty directory> --version <n> --env <env>
   ```

   **Sign:** a broken version fails with the message above. A version that shares the missing
   file fails the same way, and all of them must go. If none fails, there is nothing to repair.
   Then open `artifacts/<id>/manifests/<n>.json` of a broken version in storage: the files in it
   whose `blobs/<sha256>` is absent are the missing files.
2. **Look at the manifest objects the catalog does not know.** List `artifacts/<id>/manifests/` in
   storage. Every `<n>.json` whose number step 1's list does not show (it shows deleted versions
   too) is one. Decide each as "A manifest the catalog does not know" says. An object that names none
   of the missing files: keep it. One that names a missing file, when nobody needs its version:
   **copy it and its files out of storage first**, then remove it. One that names a missing file,
   when somebody needs its version: **stop sign 1**. **Sign:** the objects you removed are gone from
   the listing. If more than 20 such objects are left, or one is larger than 16 MiB or not valid
   JSON: **stop sign 2**, before step 3.
3. **Delete each broken version.** First copy out of the live storage the files of the version that
   you still want: the delete removes them, except those another version has, and that cannot be
   undone. Then delete the version:

   <!-- not tested: deletes a version of a restored instance; the names are placeholders -->
   ```bash
   lakana artifact versions delete <name> <n> --env <env> --yes
   ```

   That forgets the file's record, and it also removes the other files of that version, except
   those another version has. **Sign:** `Deleted version <n> of <env>/<name>.` and `deleted_at`
   set in the list. If a version that shares the file is not deleted, the file's record stays.
4. **Let one sweep run, only if step 2 left an unknown manifest object, or in the second round
   (step 5).** While one exists, the delete frees no record in its own request (its answer says
   `Freed no files` and `Kept … that Lakana's periodic clean-up decides about`); the sweep does. If
   you set `LAKANA_RUN__SWEEP_INTERVAL` to `0`, set it back to its normal value (3600) in
   `compose.yaml`, and apply it as under [Settings](#settings): `docker compose restart` does not
   read the file again, so run the `docker compose` command of the [Quickstart](#quickstart) again.
   If the value was not changed, restart the workers instead. The worker that holds the scheduler
   role runs a sweep at its first step. Then read the worker's log:

   <!-- not tested: reads the log of a running Compose stack -->
   ```bash
   docker compose logs worker
   ```

   **Sign:** a line `Artifact sweep: <n> expired uploads, <m> unneeded file records, ...` with
   `<m>` of 1 or more, **printed at any time after step 3**: the record is freed. The hourly sweep
   may have run before you got here, so look at the whole log since step 3, not only after the
   restart. A line with `0 unneeded file records` is not that sign. The sweep prints its summary
   only when it removed something, kept an artifact whole, found one damaged or failed on one: a
   sweep that found nothing to do prints nothing. If a
   sweep has run since step 3 and there is no such line, there was nothing left to free: go on to
   step 5, whose download is the check. If instead you see `The artifact sweep left '<name>'
   whole for this run: ...`: **stop sign 2**. If the line contains `stopped at its time limit`,
   the run stopped before it finished; a later run starts again, one interval later.
5. **Run the asset again and download the new version.** Run the asset the way you always do,
   then download the new version into an empty directory:

   <!-- not tested: needs a restored instance; the names are placeholders -->
   ```bash
   lakana artifact download <name> <empty directory> --version <new n> --env <env>
   ```

   **Sign:** `Downloaded <env>/<name> version <n> to <directory>`. That ends the repair: let your
   CI run again. If the download fails with the same message, do not run the asset again: the new
   version is broken too, and an upload in progress that names the file keeps its record until the
   upload expires (`LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS`, an hour by default; a run that began
   before step 3 leaves such an upload). Wait that long, then go back to step 1 for **the second
   round**, which includes step 4 even when step 2 left no object. If the second round fails the
   same way: **stop sign 3**.

**Stop sign 1: an object the catalog does not know names a missing file, and somebody needs its
version.** Keep the object, its files and every broken version of the artifact. While the object
exists the missing file's record stays, and the asset cannot store the file again. Lakana has no
command to bring such a version back yet ([issue 68](https://github.com/lakana-labs/lakana/issues/68)).
What keeps everything: copy the missing file back from a storage backup (first case above). If you
decide later that nobody needs the version, remove the object as step 2 says and go on at step 3.

**Stop sign 2: the sweep leaves the artifact whole, or step 2 found more than 20 objects or an
unreadable one.** Keep everything of the artifact: the sweep removes nothing of it and the asset
cannot get a working version. What keeps everything: copy the missing file back from a storage
backup (first case above). Or wait for [issue 68](https://github.com/lakana-labs/lakana/issues/68).
Or reduce the objects yourself, each removal being a decision of step 2 (copy first): get to at
most 20 objects that are all readable, then go on at step 3.

**Stop sign 3: the second round failed too.** Keep what is left and do not run the asset again:
each run stores another broken version. Report it in [issue
68](https://github.com/lakana-labs/lakana/issues/68) with the output of `lakana --output json
artifact versions list` and the worker's log lines of the sweep.

## Revoking the bootstrap credential

The credential `init` saves into the `team` profile (`lakana_pat_<key id>_<secret>`,
`LAKANA_BOOTSTRAP_PAT_KEY_ID`/`LAKANA_BOOTSTRAP_PAT_SECRET` in `.env`) is a normal personal access token for the
bootstrap admin principal (named `admin` by default) — it stays valid until you revoke it. The
server never prints it in its log.
Create a named admin to replace it, then revoke the bootstrap one. To add an ordinary user instead,
see [Give a second person access](give-a-second-person-access.md):

<!-- not tested: revokes the bootstrap token of a Docker Compose bundle; `<LAKANA_BOOTSTRAP_PAT_KEY_ID>` is in the reader's own `.env` -->
```bash
lakana principal create alice --type user
lakana token issue alice alice-laptop                        # shown once — save it
lakana grant create --kind INSTANCE --principal alice --role ADMIN
lakana login --profile team --url http://127.0.0.1:8000       # overwrite the team profile's own token with alice's
lakana token revoke admin <LAKANA_BOOTSTRAP_PAT_KEY_ID> --yes
```

**`--yes` is required here, not optional, without a TTY.** `token revoke` asks for
confirmation interactively; scripted or non-interactive runs (a provisioning script, CI)
need `--yes` to skip the prompt (`packages/lakana/src/lakana/cli/tokens.py`).

**Every later `docker compose up` keeps working after this.** `setup` re-runs on every
`up`, still using the now-revoked bootstrap token from `.env` — it treats a 401 or 403 from
the server as the credential having been rejected on purpose (or, less commonly, `.env` not
matching this database's admin at all — see "`setup`" below), prints a single line, and
exits 0 rather than failing the container, so `up -d --wait` still succeeds.

**`--profile team` on `lakana login` is not optional here.** Without it, `login` writes a
*new* profile (named `default`, since none of the global `--profile`, `LAKANA_PROFILE`, or an
explicit `--profile` set a name) instead of updating `team` — the `team` profile keeps the
bootstrap token, every command in this guide (and every operator habit built on it) still
uses `team` by default, and revoking `admin`'s token in the last step then breaks all of them.
`login` takes its own `--profile` flag (`packages/lakana/src/lakana/cli/auth.py`), so it goes
after `login`, not before it.

Only a principal with the `ADMIN` role on the instance can hand out access, change groups and
issue or revoke tokens, their own tokens included. The three permissions behind this
(`MANAGE_GRANT`, `MANAGE_GROUP`, `MANAGE_TOKEN`) cannot be granted on their own: a request for one
is refused with `MANAGE_GRANT comes only with the ADMIN role on the instance; grant that role
instead, or nothing.` Anyone else who tries to use one gets `MANAGE_GRANT needs the ADMIN role on
the instance. Ask an administrator to do this, or to give you the role`, followed by the command
to run. A single-action grant of one of them that was made before this rule has no effect;
`lakana grant list` marks it `no effect` in a `note` column (`--output json` has the full note,
`no effect: <ACTION> needs the ADMIN role`). See
[Upgrade policy](../explanation/upgrades.md#delegation-needs-the-admin-role).

## Starting over

This is for a Compose deployment. For the instance on your own machine, stop it and run
`lakana server reset --local` (see "Getting started"); that command refuses a Compose or remote
instance.

<!-- not tested: deletes the volumes of a Docker Compose stack, and the example runner has no Docker -->
```bash
docker compose down -v
```

then delete the bundle directory. `down -v` removes the named volumes (`pgdata`, `seaweed`),
so this discards the catalog and all stored data — there is no undo short of a backup.

`down -v` leaves what the launcher made: its environment and cache volumes, and any task or
build container (with its network) that was still running when it stopped. These commands remove
them, containers first (use your project name if it is not `lakana`):

<!-- not tested: needs a running bundle; scripts/compose_smoke_test.sh runs the volume command at its end -->
```bash
docker ps -aq --filter "label=lakana.launcher.stack=lakana" | xargs -r docker rm -f -v
docker network ls -q --filter "label=lakana.launcher.stack=lakana" | xargs -r docker network rm
docker volume ls -q --filter "label=lakana.launcher.stack=lakana" | xargs -r docker volume rm
```

## The image

`${LAKANA_IMAGE}` is one image (`Dockerfile`, repo root) that runs the server, the worker, and
`setup`, differing only in the command Compose passes each:

- **Python 3.14** (`python:3.14-slim`). With the docker runner, the bundle's default, Python
  tasks run on the task images instead (`LAKANA_TASK_IMAGE`, one per Python from 3.11 to 3.14).
  With `LAKANA_RUNNER=local` a project with Python assets must admit 3.14 in `requires-python`;
  the worker's virtual environments use this interpreter.
- **Runs as uid 10001** (`groupadd`/`useradd --system --uid 10001`), never root, with
  `HOME=/home/lakana`.
- **Ships `uv`** (`/usr/local/bin/uv`, MIT OR Apache-2.0), copied from the pinned
  `ghcr.io/astral-sh/uv` image by tag and digest, for the worker's Python-asset venvs. The
  worker's cache directory `/home/lakana/.cache/lakana-worker` exists, owned by uid 10001, mode
  `0700` (see "Python assets").
- **tini is PID 1** (`ENTRYPOINT ["/usr/bin/tini", "--"]`), so `docker stop` forwards
  `SIGTERM` immediately and reaps the actual process cleanly — no shell wrapper delaying the
  signal until the stop timeout expires.
- **DuckDB's `httpfs` extension is preinstalled** at build time
  (`duckdb.connect().execute('INSTALL httpfs')`), so the worker never downloads an extension
  the first time it runs a query.
- **Dependencies are locked.** The image is built from wheels pinned by `uv.lock`
  (`uv export --frozen ... -o dist/requirements.txt`, installed with `pip install
  --require-hashes` in the build), so an image build installs exactly the versions and files the
  repo's lockfile pins, not whatever is newest on PyPI that day; pip refuses a file whose hash
  differs. The base image is pinned by digest.
- **`HEALTHCHECK` probes `http://127.0.0.1:<LAKANA_PORT>/healthz`.** `LAKANA_HOST` (the address
  `lakana-server` itself binds to inside the container) must stay `0.0.0.0` or a loopback
  address — the image sets `LAKANA_HOST=0.0.0.0` by default — or the in-container healthcheck
  can no longer reach it and Compose will report the container unhealthy even though it's
  otherwise fine.
- **A read-only root filesystem** (for example running this image under Kubernetes instead of
  Compose, tracked as #34) needs `/var/tmp/lakana` (`TMPDIR`) and `/home/lakana` (`HOME`) mounted
  as writable volumes — everything else the image touches is read-only-compatible.

## Behind a proxy or with an internal CA

Workers fetch `https` repositories with git, which never sees the container's `HTTPS_PROXY`
or CA variables. Set `LAKANA_GIT__HTTPS_PROXY`, `LAKANA_GIT__NO_PROXY` and `LAKANA_GIT__CA_BUNDLE`
instead, by hand in `compose.yaml` on the `worker` service only (the server and `migrate` need
neither), and mount the CA file read-only. **Add** the lines to the `worker` service's
`environment:` and `volumes:` and keep every entry that is there (the worker's cache, the
launcher's socket and the task volume are needed by Python assets); `worker-interactive` and
`worker-pipeline` take both lists from `worker`:

```yaml
x-lakana-env: &lakana-env    # unchanged: the bundle's shared settings
  LAKANA_DATABASE__URL: postgresql+pg8000://lakana:${POSTGRES_PASSWORD}@postgres:5432/lakana
  LAKANA_SECRETS__MASTER_KEY: ${LAKANA_SECRETS__MASTER_KEY}

services:
  worker: &worker
    # ... (image, command and the rest unchanged)
    environment: &worker-env
      <<: *lakana-env
      LAKANA_REPO_ROOTS: '["/repos"]'
      LAKANA_RUN__SERVER_URL: http://server:8000
      LAKANA_RUN__RUNNER: ${LAKANA_RUNNER:-docker}
      LAKANA_GIT__HTTPS_PROXY: http://proxy.corp:3128        # added
      LAKANA_GIT__NO_PROXY: gitlab.corp,.internal            # added
      LAKANA_GIT__CA_BUNDLE: /certs/corp-ca.pem              # added
    volumes:
      - ./repos:/repos:ro
      - worker-cache:/home/lakana/.cache/lakana-worker
      - launcher-socket:/run/lakana-launcher
      - task-work:/home/lakana/.cache/lakana-worker/attempts
      - ./certs/corp-ca.pem:/certs/corp-ca.pem:ro            # added
```

The worker refuses to start if the CA file is not a readable file; the proxy must be an
`http(s)://` URL, with `user:password` percent-encoded if it has credentials.
Details and limits (SSH is unaffected): [Git network settings](../reference/network-settings.md#git-network-settings).

## Known gaps

The bundle's known gaps (SeaweedFS's internal APIs, a venv build that a shutdown does not interrupt) are in
[Known gaps of the Compose bundle](../explanation/compose-known-gaps.md).

Task containers share the host's kernel: they isolate projects from each other and from the
worker, they are not a sandbox for hostile code. The rest of what they do not cover is in
[Run Python tasks in containers](run-python-tasks-in-containers.md#what-it-does-not).

## See also

[The security overview](../explanation/security.md) and the [threat model](https://github.com/lakana-labs/lakana/blob/main/docs/security/threat-model.md) describe what `.env` and the
bootstrap credential protect in team mode, and what team mode explicitly does not defend
against.

If you run an instance for other people, [Run Lakana for other people](operate-for-others.md) puts these
steps in the order an operator needs them.
