# Upgrade policy

This page says what to expect when you move to a newer Lakana release, and what every upgrade
asks of you. It describes what is true today, before the public beta. For what the maintainer
promises and does not promise, see [Support](support.md).

## Versions

- Lakana is at 0.x. The four packages (`lakana`, `lakana-core`, `lakana-server` and
  `lakana-worker`) share one version, and the server, the worker and the CLI of one release belong
  together.
- **A minor release (0.N.0) may contain breaking changes**, until v1.0.0. A breaking change in a
  commit makes the next release a minor one, not 1.0.0.
- **A patch release (0.N.P) is for fixes** and is not meant to break anything. For the platform
  REST API a check enforces it: a breaking change of the API cannot land unless it is marked
  breaking, and a marked change makes the next release a minor one ([The REST API as a
  contract](../reference/rest-api.md)). Elsewhere nothing technical stops a breaking change from
  landing in a patch release: a change counts as breaking only when its author marks it so. Read
  the changelog entry of every release you skip.
- Fixes go into the latest release only (see [Support](support.md)). Upgrade first, then report.

## Where a breaking change is announced

- **The changelog.** Each release in
  [`CHANGELOG.md`](https://github.com/lakana-labs/lakana/blob/main/CHANGELOG.md) has a section
  "BREAKING CHANGES" when it breaks something. Each entry says what changed and how to migrate.
  Read it before you upgrade, for every release between yours and the new one.
- **A how-to page, when a release needs steps.** A short entry cannot carry an order of steps and
  a way back, so such a release has its own page. [Upgrade to the release with
  artifacts](../how-to/upgrade-to-artifacts.md) is the example.

## What every upgrade asks

1. **Back up first.** Take the Postgres dump and the storage backup of
   [Backups](../how-to/deploy-with-compose.md#backups).
2. **Upgrade the server and every worker together.** A worker of another release refuses some
   tasks. Do not leave an old worker running next to the new
   server.
3. **Run the database migrations before the new server starts.** The Compose bundle does that: its
   `migrate` service runs first, and the server and the workers wait for it, when you run
   `docker compose pull && docker compose up -d --wait` as in
   [Upgrading](../how-to/deploy-with-compose.md#upgrading). A local instance
   (`lakana server start --local`) migrates when it starts.
4. **Read the changelog entries**, and do what each says.

## What every minor release asks of projects with Python assets

Before v1.0.0 the `lakana` in a project's `uv.lock` must have the same minor version as the
instance. After every minor upgrade, in each project that has a Python asset (a `.py` file under
`assets/`):

1. In `pyproject.toml`, pin `lakana[assets]` to the new version.
2. Run `uv lock`.
3. Commit `pyproject.toml` and `uv.lock`, and deploy the project again.

Until then, each of that project's Python tasks fails with a message that ends like this, after
naming the two versions:

<!-- output -->
```text
This project version was deployed for another platform version: update the lock and redeploy.
```

A project with only SQL assets is not affected. A patch release does not ask for this. The full
steps, with the commands, are in
[Upgrade to the release with artifacts](../how-to/upgrade-to-artifacts.md#the-order), step 5.

This step is a known cost. [Issue 73](https://github.com/lakana-labs/lakana/issues/73) looks at
reducing it. From v1.0.0 the check only requires the major versions to match.

## When a dependency's new release breaks Lakana

Lakana uses other Python packages, such as `sqlglot` and `pyiceberg`. A `pip` or `uv` install
looks up the newest allowed version of each one when you install, so a release of such a package
that came out after the Lakana release you installed can break it, even though that Lakana
release worked the day it was published. Lakana limits the versions of the packages it depends
on most, and a daily test run looks for breaks, but one can still get through. The container
images are not affected: they install exactly the versions that were tested.

If Lakana fails after a fresh install with an error that names another package (a missing or
unexpected argument, for example), pin that package to the version before the one that broke. Use
`pip`, `uv`, or a constraints file that keeps the pin for every later install:

<!-- not tested: needs a package version that is known to be good -->
```bash
pip install "lakana[server]==0.13.0" "sqlglot==30.21.0"

uv pip install "lakana[server]==0.13.0" "sqlglot==30.21.0"   # inside the virtual environment, or add --system

echo "sqlglot==30.21.0" > constraints.txt
pip install -c constraints.txt "lakana[server]==0.13.0"
```

Replace the versions with yours: `pip freeze` lists what is installed, and the package's page on
PyPI lists its earlier releases. Then [open an issue](https://github.com/lakana-labs/lakana/issues)
with the package, the version that broke and the error. A release of Lakana that is already
published cannot be repaired, so the fix comes in a new release, and you can remove the pin then.

## Going back

A downgrade is not generally supported. No `lakana` command downgrades an instance. The way back
that always exists is to restore the backup you took before the upgrade, with
[After a restore](../how-to/deploy-with-compose.md#after-a-restore), on the previous image: it
loses everything changed since. For the release with artifacts, a manual downgrade of the database
migration also exists, and its page says what it loses: see
[Going back](../how-to/upgrade-to-artifacts.md#going-back).

## What v1.0.0 changes

v1.0.0 is the API freeze, planned for the hardening milestone of the
[roadmap](roadmap.md), which also lists upgrade paths. From then on the API stops changing in ways
that break your code ([Support](support.md)). This page has nothing more to say about it yet.

## Upgrade notes of the last releases

Each cell is a summary. The page named in the last column has the detail. The changelog has an entry for every release from 0.12.0 on; earlier releases were developed before the repository was public and have none.

| Release | What it asks | Where |
|---|---|---|
| Next release | No migration. `key:` is allowed with every write mode; on an asset that does not merge it has at most 8 columns. A project that declares `kind`, `grain`, `key`, `references` or `checks` (every project with a `write: merge` asset) deploys a version 9 manifest: upgrade every worker before you deploy it. | [Notes for the next release](#notes-for-the-next-release) |
| 0.21.0 | Migrations `0049` (connections and their tests, the secret use `connection`), `0050` (the key of a merge run) and `0051` (the record of a deploy's job starts, and the CI trust rule setting `start_on_deploy_jobs`). **Breaking:** a REST client that reads every job trigger kind must ignore or handle the new `{"kind": "deploy"}` (a job with `on_deploy: true` starts after each deploy; CI trust rules opt in with `start_on_deploy_jobs`, off on every existing rule; upgrade the workers before you deploy such a job); a Python asset's parameter `ctx` is its context, never an input (rename such an input); an unusable `LAKANA_RUN__CA_BUNDLE` stops the worker, and local tasks trust Mozilla's roots plus that file instead of the system store; builds and tasks never reach a Docker gateway or `host.docker.internal` (list the host's other addresses in `LAKANA_EGRESS_REFUSED`); under the local runner a database connection test needs `lakana[assets]` next to the worker. Upgrade the launcher, `egress`, the task images (46.7 MB larger each) and the workers together, and the workers before you deploy connections or `write: append`/`merge`. Lock Python projects again: `lakana[assets]` gains six packages (about 19 MB of wheels). An http connection test that gets 5xx or 429 is `failed`. Python tasks need this release's server: a worker of this release next to an older server fails every Python task (`… holds no write of this attempt`). New: connections, task egress, the `lakana.ingest` helpers, `lakana load`, `lakana init --template`, `@asset(evolve_schema=True)` for a Python asset that replaces its table, and `LAKANA_INGESTION_ROOTS` in the bundle's `.env`. | [Notes for 0.21.0](#notes-for-0210), [Load data from a source](../how-to/load-data-from-a-source.md) |
| 0.20.0 | No migration. **Breaking:** the Compose bundle runs Python tasks in containers: replace `compose.yaml`, add four `.env` lines, Docker Engine 28 or later; budget `LAKANA_RUN__TASK_MEMORY` (2 GB) per worker; with the docker runner `LAKANA_RUN__HTTPS_PROXY`, `LAKANA_RUN__CA_BUNDLE` (accepted again from the next release) and an `http://` `LAKANA_RUN__INDEX_URL` are refused, and a registered index on another port than 443 needs a credential. A protected environment runs Python assets only on workers with `LAKANA_RUN__RUNNER=docker`, so no longer in local mode. | [Notes for 0.20.0](#notes-for-0200), [Run Python tasks in containers](../how-to/run-python-tasks-in-containers.md) |
| 0.19.0 | No migration. **Security fix**: upgrade the server and every worker. **Breaking:** queries run on DuckDB 2.0: `//` and `%` by zero fail (use `TRY` or `NULLIF`), an untyped `NULL AS x` in a write is refused (add a `CAST`), a table function's alias is its value, `price = 1.1` after a float-to-double widening matches only newer rows, some Parquet files from other writers may not read; the worker needs macOS 10.15 or glibc 2.27. A `PIVOT` lists its values; `CONNECT BY`, DuckDB-internal and sequence functions are refused; a query opens only the tables it reads. The worker stops at start-up on an unusable CA bundle; an unverifiable store certificate fails at once. | [Notes for 0.19.0](#notes-for-0190) |
| 0.18.0 | Migrations `0047` (CI trust rules and CI tokens) and `0048` (measured sizes), both adding only empty tables and columns. A new environment's storage root must be in the environment's scheme, name a bucket or container, and may not be `file:///`, contain `::`, or lie in another environment's files (422 with a root to use; existing environments are not changed). An allowed webhook host may no longer reach loopback or cloud metadata addresses, except the entry `localhost`. Log lines worth an alert end with `event=<code>`. Copy the new healthchecks from a fresh `compose.yaml`. The workers trim uv's package cache above 10 GiB (`0` keeps it untrimmed). `lakana init` writes CI files that deploy without a stored secret; `--ci-auth pat` for the old ones. | [Notes for 0.18.0](#notes-for-0180) |
| 0.17.0 | Migrations `0042` to `0046`. **Breaking:** a dropped table's files, and those of the tables of a deleted environment, are deleted after `LAKANA_TABLES__UNDO_DAYS` (7 days; restore with `lakana table undrop` or `lakana env restore`), and `DELETE /api/v1/environments/{name}` answers 200 with the environment; the first administrator's token is written to `LAKANA_BOOTSTRAP_TOKEN_FILE`, never to the log; ADMINs no longer see other people's SQL text or column values in the audit log until granted `READ_QUERY_TEXT`. Set `LAKANA_TABLES__UNDO_DAYS` the same on the server and the workers. A new storage root must be written canonically. Nothing expires unless retention is set. Sign-in events are written once per token every 15 minutes. | [Notes for 0.17.0](#notes-for-0170) |
| 0.16.0 | Migrations `0036` to `0041`. `GET /api/v1/grants` without a filter now returns every grant, in pages of 100 (follow `X-Next-Cursor`), and the principal and group lists come newest first, paged the same way. Filters and masks that exist are not checked again; a new one is type-checked when it is created. Descriptions appear after the next deploy; table readers count from the upgrade on; search finds the columns of existing tables once the sweep has filled them in (1,000 tables take about 20 hours). Upgrade the workers together with the server, so that every task failure gets its code. | [Notes for 0.16.0](#notes-for-0160) |
| 0.15.0 | Migration `0035` (a database check on a secret's use; it stops and names any inconsistent secret). While an environment has a storage secret, changing its storage options needs `MANAGE_SECRET` there as well as `MANAGE_ENVIRONMENT`; the Compose set-up attaches its storage secret to `dev` only together with the bundle's own options. A commit through the Iceberg REST catalog is refused when one of its manifest files is over 64 MiB. | [Notes for 0.15.0](#notes-for-0150) |
| 0.14.0 | Migration `0034` (binds each secret to one use). New images are `ghcr.io/lakana-labs/lakana`: change `LAKANA_IMAGE` in a bundle's `.env` before you pull. Replace the bundle's `compose.yaml`: SeaweedFS runs as two containers behind an S3 gateway, and `docker compose down` must come before `up`. A secret has one use (a package index, a repository, or its own environment) and is refused for any other; a secret the migration could not bind is bound by an administrator with `lakana secret bind`. | [Notes for 0.14.0](#notes-for-0140) |
| 0.13.0 | Migration `0033` (adds `schema_id` to row filters and column masks). Iceberg tables are read by field id: a renamed column keeps its data; a column dropped and added again, also by a type change with `evolve_schema`, is a new column whose older rows read as NULL; reads of a table whose nested fields were renamed or re-added, that has a column type Lakana cannot read, or whose column names differ only in letter case are refused. A commit through the Iceberg REST catalog that adds a data file without field ids is refused, as is one of more than 2,000 data files or one whose check takes over 60 seconds; files committed earlier without field ids read as NULL. A read under a row filter or column mask is refused when a column the policy uses was renamed or replaced since the policy was created: create the policy again for its column, then revoke the old one. A query that changes data is refused wherever the statement sits. Errors on reads under a row filter or column mask show a fixed message. | [Notes for 0.13.0](#notes-for-0130) |
| 0.12.1 | No migration. Columns qualified with the table's name work, also under a row filter or column mask. | [Notes for 0.12.1](#notes-for-0121) |
| 0.12.0 | Migrations `0020` to `0032`. Before you upgrade, check that an administrator can sign in. A grant of `MANAGE_GRANT`, `MANAGE_GROUP` or `MANAGE_TOKEN` as a single action no longer has an effect: only the `ADMIN` role does. A project whose `uv.lock` names a package index that is not registered, or a git or URL dependency, stops at its next run: an administrator registers the index, and the project declares it with `explicit = true`, is locked and deployed again. The worker ignores a project's `[tool.uv]` settings. Every query runs in a process of its own: a query past its time limit is ended, a query can be cancelled and can end as `CANCELLED`, and a worker over its memory limit ends its largest query or Python task before it stops itself. If you search the server's or the worker's log: a record of several lines now has `    | ` at the start of its later lines, control characters are written as escapes (a carriage return as `\x0d`), and uvicorn's level names have no colours. A query or a deploy that an earlier release left `RUNNING` for ever is ended within about a minute and shows `FAILED` with the cause `worker_lost` (a deploy shows `failed`); nothing is run again by itself. Stop every old worker before you migrate and start the new ones after: a worker of the earlier release does not register, so the new scheduler ends its queries and deploys after a lease. `lakana query list` and `lakana worker list` are new. With three or more workers that take ad-hoc queries, one user can have at most all but one of them busy at once (three workers: two queries running at once; four: three); the Compose bundle's default of two workers is not affected. Data quality checks are new, and a check can keep the data that failed it (`on_failure: keep`); a project can set the default of its checks in `lakana.yaml`: a project without a `checks/` directory runs unchanged, one with checks needs every worker upgraded with the server, and a commit through the Iceberg REST catalog that would leave a table with no current snapshot is now refused. `lakana check rows` shows the rows that a check found, and `lakana table maintain` removes the failed snapshot of a check after seven days. | [Notes for 0.12.0](#notes-for-0120), [Data quality checks](#data-quality-checks), [Workers for one kind of work, and a limit per user](#workers-for-one-kind-of-work-and-a-limit-per-user), [Use a private package index](../how-to/private-package-index.md), [Queries run in a process of their own](#queries-run-in-a-process-of-their-own), [Queries and deploys cannot get stuck](#queries-and-deploys-cannot-get-stuck), [Control characters in the log](../how-to/deploy-with-compose.md#the-sweep) |
| 0.11.0 | Migrations `0018` and `0019`; set every secret you created again; every worker upgraded first; lock Python projects again; new limits on request bodies and SQL text; workers stop at 4 GB. | [Upgrade to the release with artifacts](../how-to/upgrade-to-artifacts.md) |
| 0.10.0 | A package index URL with a query string is refused; move a `uv.toml` into `pyproject.toml`; lineage and failure causes changed. | This row |
| 0.9.0 | Upgrade every worker before you deploy Python assets; rewrite `asset(deps=...)` as `asset(inputs=...)`; move helper modules out of `assets/`; an older bundle needs three additions. | [Upgrading](../how-to/deploy-with-compose.md#upgrading), [Projects and deploys](../how-to/projects.md) |

## Notes for the next release

### Say what a table is in its asset's header

An asset may declare `kind` (`fact`, `dimension`, `wide` or `staging`), `grain`, `key`,
`references` and `checks`, in its `/* lakana … */` block or as arguments of `@asset(...)`. A deploy
checks them against the rules of the kind and resolves each reference against the project and the
environment; a target you may not read gets the same message as one that does not exist. See
[Model fields](../reference/asset-front-matter.md#model-fields-kind-grain-key-and-references).

- `key:` is now allowed with every write mode: it says which columns identify a row. `write:
  merge` uses the same key.
- The key of an asset that does not merge has at most 8 columns; a deploy refuses a longer one.
  A merge key keeps the rule of the previous release.
- When a run principal is bound to the repository, a deploy also checks that it may read every
  reference target outside the project, and names the run principal when it may not.
- A project that declares a model field deploys a version 9 manifest. `key:` alone counts, so
  every project with a `write: merge` asset deploys version 9 from this release on: upgrade every
  worker before you deploy it. A worker of an earlier release refuses the manifest.

## Notes for 0.21.0

### Python tasks: builds that cannot succeed fail at once, and a task proves its write

An environment build fails for good, without retries, when a package's own build from its source
fails or a package has no file for the worker's platform: every attempt would build it the same
way. Depend on a version with a wheel for the platform, run `uv lock` and deploy again. A build that
failed on the network is still retried. An asset that reads its own table, which does not exist
yet, now fails before its environment is built.

A Python task now counts as succeeded only when the table holds its own write: each snapshot a task
writes carries `lakana.task-run-id` and `lakana.task-claim` in its snapshot summary, and a change
somebody else made fails the attempt with `… holds no write of this attempt: another writer changed
it. …` (cause `conflict`). An append or merge that found nothing new still succeeds. A snapshot
summary that a client sends through the Iceberg REST catalog or the API loses both properties: only
the platform sets them.
The server sets both properties; a worker of this release next to an older server fails every
Python task that way, so upgrade the server and the workers together, as always. In the Compose
bundle a new worker can start a few seconds before the new server answers; a Python task that ends
in that window fails this way, and running it again is enough. See
[Python assets](../reference/python-assets.md#failure-retry-and-cancel).

### Python assets can change their table's columns

A Python asset that replaces its table may say `@asset(evolve_schema=True)`: each run then gives
the table the columns the function returns, with the rules of a SQL asset's `evolve_schema`, and a
column it no longer returns is removed from the table. Without it nothing changes: a changed
output schema fails the run as before, and the message now names `evolve_schema=True` as the
next step. It needs this release's `lakana` in the project's lock. A column whose type changes
without widening is a new column, so a row filter or column mask that uses it refuses its reads
until an administrator creates it again. Before you remove a column others
read, see [When a table's columns change](when-columns-change.md).

### Connections: name each source once per environment

`lakana connection create NAME --env ENV --kind KIND ...` stores a source system's address,
settings and secret once per environment; `list`, `show`, `update`, `delete` and `test` manage it
([Connections](../reference/connections.md)). Migration `0049` adds the connections, their tests
and the secret use `connection`; `0050` records the key a merge run matches rows on. Managing
connections needs `MANAGE_CONNECTION` on the environment, which `ADMIN` and `DATA_ENGINEER` carry:
existing grants need no change. An asset names the connections it reads
(`@asset(connections=["crm-db"])`), a deploy refuses a name its environment does not have, and a
running task fetches the credential with its own token, never from a variable or a file; every
fetch is audited (`lakana audit list --kind connection`). A project whose asset declares a connection
gets manifest version 7, which a worker of an earlier release refuses: upgrade the workers before
you deploy one. [Load data from a source](../how-to/load-data-from-a-source.md) walks through a
first one.

### Python assets may return an Arrow table

A Python asset may return a `pyarrow.Table` as well as a `polars.DataFrame`; the `lakana.ingest`
helpers return Arrow, so an asset can return what they read. The harness that accepts it is the
`lakana` package in the project's own environment: lock the project with this release's `lakana`
before an asset returns Arrow; an earlier one fails the run with
`the asset must return a polars DataFrame, got pyarrow.lib.Table`. See
[Python assets](../reference/python-assets.md).

### `lakana[assets]` is larger

`lakana[assets]` gains six packages for the ingest helpers: `fastexcel`, `obstore`,
`adbc-driver-manager`, `adbc-driver-postgresql`, `adbc-driver-sqlite` and `PyMySQL`. Its wheels
for CPython 3.14 on Linux x86-64 grow from 112.5 MB to 131.2 MB (download size, measured from
`uv.lock`), so each environment build downloads about 19 MB more. A project locked earlier keeps
its old lock until you run `uv lock --upgrade-package lakana`; it needs the new packages only for
the helpers. The extras themselves are unchanged (`server`, `assets`); the repository's `uv.lock`
gains a dependency group `check`, the packages of the connection check that `Dockerfile.task`
installs (see "Connection tests check database and GCS credentials" below).

### Load a local file into bronze with `lakana load`

`lakana load FILE TABLE` loads a CSV, Parquet, Arrow, JSON, NDJSON or spreadsheet file into a
table of the bronze layer once, after showing its schema and row count; a bare name means
`bronze.<name>` ([`lakana load`](../reference/load.md)). The server refuses any other layer, for
the CLI, the SDK and a raw request alike: transform bronze into silver with an asset. Nothing is
to change for an upgrade; the route needs the server and the CLI of this release.

### Start an ingestion project with `lakana init --template`

`lakana init DIRECTORY --template ingest-files`, `ingest-sql` or `ingest-rest` writes a project
with one asset that reads a source incrementally, and a README with the connection to create.
Existing projects are not changed. See
[Load data from a source](../how-to/load-data-from-a-source.md).

### Start a project with a Python asset with `lakana init --template python`

`lakana init DIRECTORY --template python` writes the sample project, one Python asset built from
two of its tables, a `pyproject.toml` that depends on `lakana[assets]`, and runs `uv lock`, so the
project deploys as written. It needs `uv` on your `PATH` and network access to the public package
index. Existing projects are not changed. See
[Build a table with Python](../tutorials/python-sample.md).

### The Compose bundle mounts ingestion roots

A bundle's `.env` gains `LAKANA_INGESTION_ROOTS`, a JSON object of root name to absolute host
directory (`{}` by default), which `compose.yaml` passes to the launcher as
`LAKANA_LAUNCHER__INGESTION_ROOTS`: each root is mounted read-only into a task whose `directory`
or `sqlite-file` connection names it. To use it, add the line to an older `.env`
(`LAKANA_INGESTION_ROOTS='{"exports": "/srv/exports"}'`), replace `compose.yaml` with this
release's, and run `docker compose up -d --wait`. A bundle whose `.env` lacks the key, or whose
`compose.yaml` is older, has no roots, as before. See
[Files on this host](../how-to/deploy-with-compose.md#files-on-this-host).

### Read files with `lakana.ingest.files`

`lakana[assets]` gains `fastexcel` (for spreadsheets) and `obstore` (for S3, GCS and Azure Blob
Storage), so a project locked earlier runs `uv lock --upgrade-package lakana` to pick them up.
`lakana.ingest.files.read` and `files.list` read CSV, Parquet, JSON and spreadsheet files from
`s3`, `gcs`, `azure`, `http` and `directory` connections, through the task's egress proxy under
the Docker runner; see [the ingest helpers](../reference/ingest.md). CSV, JSON and NDJSON files
must be uncompressed UTF-8: a compressed one is refused before it is parsed.

### Read databases with `lakana.ingest.sql`

`lakana[assets]` gains the ADBC drivers of PostgreSQL and SQLite (`adbc-driver-manager`,
`adbc-driver-postgresql`, `adbc-driver-sqlite`) and `PyMySQL`, so a project locked earlier runs
`uv lock --upgrade-package lakana` to pick them up. `lakana.ingest.sql.read` runs one read-only
query on a `postgresql`, `mariadb` or `sqlite-file` connection, with values bound to `:name` placeholders;
see [the ingest helpers](../reference/ingest.md#sql).

### Connection tests check database and GCS credentials

`lakana connection test` now logs in to a `postgresql` or `mariadb` connection with its secret
(`SELECT 1` in a read-only transaction; MariaDB over TLS as its `tls` says), opens a `sqlite-file`
connection's file read-only as a SQLite database, and checks a `gcs` key by signing a token request
to Google's token endpoint and listing at most one object. A refused login or key is
`credentials`. A `gcs` key whose `token_uri` names another host than `oauth2.googleapis.com` is
`credentials` too, and nothing is sent; store the key file Google gave you. See
[Test a connection](../reference/connections.md#test-a-connection).

**Breaking, for the local runner only.** The check runs on the worker's own Python there, and it
needs the database drivers: a test of a `postgresql`, `mariadb` or `sqlite-file` connection now
fails unless `lakana[assets]` is installed next to the worker. The message names the module and
what to install. Install `lakana[assets]` in the worker's environment, or use the Docker runner.
A `gcs` key needs nothing more: the worker already has what signs it.

Under the Docker runner nothing is to install: each task image now holds a check environment,
`/opt/lakana-check-env`, with exactly the check's drivers (installed by hash from the release's
lock, owned by root and read-only), so **each task image grows by 46.7 MB**. Pull the task
images of this release together with the launcher and the workers, as every release asks; a test
on a task image of an earlier release ends `failed` with ``the task image has no connection check
(/opt/lakana-check/check.py and its environment /opt/lakana-check-env) ...``. A task image you
build yourself needs `dist/check-requirements.txt`, the check environment's packages exported from
`uv.lock`, in its build context first; the header of `Dockerfile.task` shows both commands.

### Read REST APIs with `lakana.ingest.rest`

`lakana.ingest.rest.read` reads a JSON REST API through an `http` connection, by page number,
cursor or next link (OData's `@odata.nextLink` included), with the connection's auth and retries
within a time budget; see [the ingest helpers](../reference/ingest.md#rest). It needs no new
package.

`files.read` now refuses a JSON or NDJSON file that nests arrays and objects more than 100 levels
deep, with a `FileShapeError` naming the file: such a file could crash the task at about 10,000
levels, and no table needs more than 100.

### Connection tests check http credentials; `files.read` reads `oauth2` connections

`lakana connection test` now sends one `GET` of an `http` connection's base URL with its auth;
an `oauth2` connection first gets a token from its `token_url`. A refused credential (401 or 403,
or a token endpoint that refuses the client) is `credentials`, where the test said "credentials
not checked yet" before; a base URL that answers neither 2xx nor a refusal (a 404, say) stays
`ok`, with "credentials not refused". A base URL that answers 5xx (or 429) is now `failed`
("test again later"), where it was `ok` before. Nothing is to install, under either runner, and the task
image does not grow. `files.read` now reads an `http` connection with `auth: oauth2`, and sends a
connection's credential only to the scheme, host and port of its `base_url`. See
[Test a connection](../reference/connections.md#test-a-connection).

### Builds and tasks never reach the Docker host's gateways or names; list its other addresses

**Breaking.** Under the docker runner, builds, tasks and connection tests no longer reach the
gateway address of any Docker network (such as `172.17.0.1`, where the Docker host itself
answers) or the Docker host's well-known names (`host.docker.internal`,
`gateway.docker.internal`, `docker.for.mac.host.internal`, `docker.for.mac.localhost`,
`docker.for.win.localhost`). A package index or a source on the Docker host that a build or a
task reached that way is now refused (`... is not reachable through this proxy`): reach it by
another address of the host, one you do not list in `LAKANA_EGRESS_REFUSED`, or move it off the
host.

The host's other addresses are refused only when you list them: add every address of the Docker
host (`ip -br addr`: LAN address or subnet, VPN or Tailscale `100.64.0.0/10`, `virbr0`, and on
Docker Desktop `192.168.65.0/24`) to the Compose `.env` variable `LAKANA_EGRESS_REFUSED`
(`LAKANA_EGRESS_REFUSED=192.168.1.0/24,100.64.0.0/10`), replace `compose.yaml` with this
release's, which passes it to the launcher as `LAKANA_LAUNCHER__EGRESS_REFUSED`, and run
`docker compose up -d`. Without Compose, set `LAKANA_LAUNCHER__EGRESS_REFUSED` on the launcher
itself.

Upgrade the launcher and the workers together, as the bundle does: a task request now always
carries its `egress` and `relay` fields, so a worker and a launcher of different releases refuse
each other's requests (`the launcher refused the task: run takes exactly the fields ...`) and
every Python task fails until both run this release. Services on the
Docker host that Lakana's containers should not reach are safest bound to `127.0.0.1`. See
[Addresses builds and tasks never reach](../reference/network-settings.md#addresses-builds-and-tasks-never-reach).

### Tasks in containers reach the connections their asset declares

Under the docker runner a Python task whose asset declares connections now reaches exactly their
endpoints: HTTPS through the `egress` service, databases and `tcp` through a relay the launcher
starts for the task, directories as read-only mounts of `LAKANA_LAUNCHER__INGESTION_ROOTS`
(`LAKANA_INGESTION_ROOTS` in the bundle's `.env`, see "The Compose bundle mounts ingestion roots"
above). Before,
such a task reached the server only. A task without connections is unchanged. The `egress` service
is now needed for tasks with HTTPS or database connections, not only for builds: the bundle's
`compose.yaml` has it; without it running, those tasks fail with ``tasks with connections need
the `egress` service ...``. Upgrade the launcher, `egress` and the workers together, as with every
release (the relay runs in the `egress` service's image). See
[Connections: what a task reaches](../how-to/run-python-tasks-in-containers.md#connections-what-a-task-reaches).

### Connection tests run under the docker runner

`lakana connection test` now runs on workers with the docker runner too, the Compose bundle's
default: the launcher runs Lakana's own check in a container of the task image, with the network
rules a task with that connection gets. Before, a test waited for a worker with the local runner.
Upgrade the launcher, the task images and the workers together, as the bundle does: a launcher of
an earlier release refuses the check (`the launcher refused the check: op must be one of ...`),
and the test ends `failed` naming the upgrade. Each task image now holds the check, one file at
`/opt/lakana-check/check.py`, which the image runs on its check environment
(`/opt/lakana-check-env`); a task image you build yourself from this release's `Dockerfile.task`
has both too (see "Connection tests check database and GCS credentials" above). See [Test a connection](../reference/connections.md#test-a-connection).

### A Python asset's parameter `ctx` is not an input

A parameter named `ctx` now receives the asset's context and is never an input. A project whose
asset reads a table into a parameter `ctx` (`inputs={"ctx": "silver.orders"}`) fails its deploy
with ``` `ctx` is the asset's context; give the input another parameter name ```: rename the
parameter and its entry in `inputs`, then deploy again. A project version deployed before the
upgrade keeps reading that input into `ctx` until you deploy a new one. An asset whose function
has a parameter `ctx` but whose project is still locked to an earlier `lakana` fails with a
`TypeError` (the missing argument `ctx`), because that release's harness never passes it: lock
the project again with this release's `lakana` and deploy.

### Assets append and merge with `write:`

An asset says how a run writes its table with `write: replace` (the default), `append` (with
`watermark:`) or `merge` (with `key:`); see [Write modes](../reference/asset-front-matter.md#write-modes).
`materialize: incremental` with `incremental_key:` keeps working as the older spelling of
`write: append`, and a deploy prints a hint. A project version that spells `write: append` or
`merge` has manifest version 8, which a worker of an earlier release refuses: upgrade the workers
before you deploy such a project. A Python asset's `materialize="incremental"` is now refused
with a message that points at `write="append"`.

A Python asset that appends, merges or declares a connection receives a newer task description,
which only the `lakana` package of this release reads: its project must be locked with this
release's `lakana`, as after every upgrade (see the lock row of
[Failure, retry and cancel](../reference/python-assets.md#failure-retry-and-cancel)). Until then
such an asset fails with `the task process exited with status 2 without a result: the harness
refuses a task description it does not understand …`; an asset that replaces and declares no
connection runs as before. A Python asset that appends must
return only the rows past `ctx.watermark()`: see [The asset's context](../reference/python-assets.md#the-assets-context).

### Builds reach a package index on any port

Under the docker runner a build now reaches each package index on the port of its URL, with a
credential or without: a registered index on another port than 443 no longer needs a credential
(the limit of 0.20.0). Upgrade the launcher and the workers together, as the bundle does: a
worker of 0.20.0 and a launcher of this release (or the other way round) refuse each other's
build requests (`build takes exactly the fields ...`), and the task fails until both are
upgraded. The shared build caches get new names, so the first build of each environment after
the upgrade downloads its packages again.

### The docker runner trusts a private certificate authority

`LAKANA_RUN__CA_BUNDLE` now works with the docker runner too: builds and tasks of both runners
trust Mozilla's roots plus the certificates of that file, which the worker mounts read-only at
`/trust/trust.pem` in each build and task container and names in `SSL_CERT_FILE` and
`LAKANA_CA_BUNDLE`. In a Compose bundle, mount the file into the `worker` service read-only and
set the variable there; see
[The trust bundle of builds and tasks](../reference/network-settings.md#the-trust-bundle-of-builds-and-tasks).

**Breaking** for a worker that already sets it with the local runner:

- The worker checks the file at start. A file over 1 MiB, without a certificate, with a private
  key, with a damaged or cut-off certificate block or in OpenSSL's `TRUSTED CERTIFICATE` form
  now **stops the worker**, with a message that names the setting, the file and the cause;
  before, the local runner passed any file on. Give a PEM file of certificates only. The local
  runner also refuses to write its copy of the bundle into a cache directory other users can
  write to.
- Only the file's certificate blocks are used. Local builds trust **Mozilla's roots plus the
  file**, where before they trusted the file alone: public indexes such as PyPI are now trusted
  next to it.
- Local **tasks** used to get no `SSL_CERT_FILE`, so Python's `ssl` (and `urllib`) trusted the
  host's system store. Now `SSL_CERT_FILE` names Mozilla's roots plus the file, and that
  **replaces the host's system store** for them. A corporate authority installed only in the
  system store (`update-ca-certificates`, `update-ca-trust`) is no longer trusted by a local task:
  put every private authority your tasks need into the `LAKANA_RUN__CA_BUNDLE` file.

Upgrade the launcher and the workers together, as the bundle does: a worker of 0.20.0 and a
launcher of this release (or the other way round) refuse each other's requests
(`run takes exactly the fields ...`), and tasks fail until both are upgraded.

### A cancel stops an environment build

A cancel, a drain past its grace or a lost lease now stops a task's environment build at once,
with either runner, instead of waiting for it to end (up to `LAKANA_RUN__VENV_BUILD_TIMEOUT`,
600 s). Nothing of the stopped build is kept: the next run of the project builds its environment
again.

### Only the worker that started a task can stop it

The launcher stops a task or a build only for the worker that started it, also when a worker's
connection closes. Nothing changes for a bundle whose workers and launcher run the same release.
A worker of 0.20.0 next to a launcher of this release cannot stop its tasks (the launcher
refuses its stop request), so its cancelled tasks run until their timeout: upgrade the workers
and the launcher together.

### One build holds at most 32 connections of the egress proxy

`egress` lets one build hold at most 32 connections at once
(`LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK`), so a build cannot take the connections other
builds need; a build past it gets a `503` that names the setting. Builds now download at most
16 packages at once (uv's own default is 50), which stays within it. A build whose own code opens
many connections may need the setting raised under `egress` in `compose.yaml`; see
[The egress proxy](../how-to/run-python-tasks-in-containers.md#the-egress-proxy).

### A standard PIVOT pivots

`SELECT * FROM gold.sales PIVOT (sum(amount) FOR region IN ('eu', 'us'))` and the standard
`UNPIVOT (...)` now return pivoted rows; before, Lakana dropped the clause and returned the
table's rows unchanged, with no error. The same held for a `TABLESAMPLE` after a table name and
for the second table of a join written in parentheses (`FROM (gold.a JOIN gold.b ON ...)`). A
query, view or asset that used one of these returns different rows after the upgrade. A clause
Lakana cannot apply to a table (`AT (...)`, `ONLY`, `WITH ORDINALITY`, a table hint) is now
refused instead of dropped; see
[Clauses after a table name](../reference/sql-in-queries.md#clauses-after-a-table-name).

### An operator called by name is refused

A query that calls an operator as a function, such as `"<"(id, 'a')`, `main."="(a, b)` or
`"IS DISTINCT FROM"(a, b)`, is now refused with a message that says to write the operator
itself (`id < 'a'`). On DuckDB 2.0 such a call could fail with an internal error. A view or an
asset written this way needs the operator written as an operator.

### A date outside the timestamp range no longer fails a filtered read

A row filter that compares a date with a timestamp (`born < seen`) used to fail every read it
covered when a row held a date outside the timestamp range (a year such as 300000). Such a row is
now hidden by the filter instead, and the read succeeds. See
[Filter rows](../how-to/filter-rows.md).

### Lineage shows a moved and dropped table the same way from every table

A dropped table that had been renamed into another namespace used to be shown or hidden in
`table lineage` depending on which table you started from. It is now judged by the last name
Lakana recorded for it: it is shown, under that name, to a reader of that name's namespace, and
hidden from a reader of only its earlier namespace.

### A job can start after each deploy

A job with `on_deploy: true` in `lakana.yaml` starts after each deploy that makes another
project version current, as whoever deployed; runs started this way have the trigger `deploy`
(`lakana runs list --trigger deploy`). A deploy through a CI token starts them only when its CI
trust rule opts in: the new rule setting `start_on_deploy_jobs` (`lakana ci-trust update RULE_ID
--start-on-deploy-jobs`) is false on every existing rule. The key is off by default, so existing
jobs do not change, and a project that does not use it keeps its manifest. Migration `0051` adds
the record of these starts to each deploy (`job_starts` in `GET .../deploys/{id}`) and the rule
setting. A worker of an
earlier release refuses a project that uses the key
(`jobs.refresh.on_deploy: Extra inputs are not permitted`), so upgrade the workers before you
deploy one. A downgrade records each run with the trigger `deploy` as `api` (a service account
started it) or `manual`. See
[Start a job after each deploy](../how-to/run-a-job-on-a-schedule.md#start-a-job-after-each-deploy).

**Breaking for a REST client that reads every trigger kind:** the `triggers` of a job
(`GET .../jobs`, `GET .../jobs/{job}`, pause and resume) may now hold `{"kind": "deploy"}`, between
`schedule` and `on_demand`. The [Jobs reference](../reference/jobs.md#triggers) has always asked a
client to ignore a kind it does not know; the SDK of every release does, and this release's reads
it.

## Notes for 0.20.0

### Python tasks run in containers in the Compose bundle

The bundle gains two services, `launcher` and `egress`, and runs every Python task in a container
of its own (`LAKANA_RUNNER=docker` in `.env`). An existing bundle needs the new `compose.yaml`
and four `.env` lines (see [Upgrading](../how-to/deploy-with-compose.md#upgrading) on the Compose
page); Docker Engine 28 or later is required. Task containers sit outside the worker's memory
limit: budget `LAKANA_RUN__TASK_MEMORY` (2 GB) per worker on top of the workers' own. Builds reach
package indexes only through `egress`: a corporate proxy goes in `LAKANA_EGRESS_UPSTREAM_PROXY`,
and `LAKANA_RUN__HTTPS_PROXY` and `LAKANA_RUN__CA_BUNDLE` are refused with the docker runner, and
so is an `http://` `LAKANA_RUN__INDEX_URL` (the next release accepts `LAKANA_RUN__CA_BUNDLE` with
the docker runner: see
[The docker runner trusts a private certificate authority](#the-docker-runner-trusts-a-private-certificate-authority)). A registered package index on another port than 443
that has no credential, which builds of the local runner reached, is refused (403 from `egress`)
under the docker runner until [#301](https://github.com/lakana-labs/lakana/issues/301): give it a
credential or serve it on port 443.
`LAKANA_RUNNER=local` runs tasks as the worker's own processes again, as before, but the new
`compose.yaml` still starts `launcher` and `egress`: Docker Engine 28, the Docker socket and
`LAKANA_TASK_IMAGE` stay required, and the workers wait for the launcher (to drop both services,
edit `compose.yaml` by hand, see [Python assets](../how-to/deploy-with-compose.md#python-assets)).
Local mode is unchanged. What containers isolate and what they do not, every setting and every
message: [Run Python tasks in containers](../how-to/run-python-tasks-in-containers.md).

### A protected environment runs Python assets only in containers

A worker with `LAKANA_RUN__RUNNER=local` now fails a Python task of a protected environment at
once, with "environment 'prod' is protected, and this worker runs Python tasks without isolation
(LAKANA_RUN__RUNNER=local). …" (failure code `no_runtime`). Before you upgrade, give every worker
that takes pipeline work the docker runner, or a protected environment's Python assets stop
running. Local mode runs tasks without containers, so it can no longer run the Python assets of a
protected environment; unprotect it there (`lakana env unprotect`) if you rely on that. SQL assets
are not affected.

## Notes for 0.19.0

**Security fix: upgrade the server and every worker.** This release closes a way for a reader to
get around row filters and column masks; the security advisory published with the release has the
details.

### Queries run on DuckDB 2.0

**Breaking.** The worker runs every query, view, check and SQL asset on DuckDB 2.0 (a release
with long-term support, until at least October 2027) instead of DuckDB 1.5. The server, the CLI
and the SDK do not use DuckDB. Most SQL runs unchanged; the items below say what does not, and
what to write instead. Each refusal names its cause in the run's error message.

**Installing the worker**

- **Python 3.11 or newer, as before.** DuckDB 2.0 needs macOS 10.15 or newer (was 10.9) and, on
  x86_64 Linux, glibc 2.27 or newer (was 2.26). As before, there are no wheels for musl (Alpine
  Linux): run the worker from the container image or on a glibc distribution.
- **One more download for a worker installed with `pip`.** Each DuckDB release has its own build
  of the `httpfs` extension, so such a worker downloads it once more on its first query (from
  `extensions.duckdb.org`; see [What leaves the machine](what-leaves-the-machine.md)). The
  container image already contains it. On a machine without internet access, put the extension
  for DuckDB 2.0.0 into the worker's extension directory before the first query.

**SQL that behaves differently** (details in [SQL in queries](../reference/sql-in-queries.md))

- **Integer division and remainder by zero fail.** `x // 0` and `x % 0` returned `NULL`; now the
  query fails with "Division by zero in expression (...)". Write `TRY(x // y)`, or divide by
  `NULLIF(y, 0)`, where a divisor can be zero.
- **A column that is only `NULL` needs a type in a write.** `SELECT NULL AS x` into a table is
  refused with "Column 'x' is NULL without a type, which a Lakana table can't store; write
  `CAST(NULL AS <type>) AS x`, for example `CAST(NULL AS VARCHAR)`." (DuckDB 1.5 created an
  integer column.) Write the `CAST`. A read and a view are not affected.
- **A table function's alias is its value.** In `FROM generate_series(1, 3) i` (also `range`
  and `unnest`), `i` is now the number, not a struct with one field. `i.generate_series` keeps
  working; SQL that treated `i` as a struct, such as `i['generate_series']`, now fails: write `i`.
- **Equality on a column widened from `float` to `double`.** `price = 1.1` now matches only the
  rows written after the widening (before, it could also match older rows, depending on how the
  condition was written). Write a range, `price > 1.09 AND price < 1.11`, or
  `round(price, 2) = 1.1`, which match old and new rows alike; a row filter on such a column
  needs the range ([Filter rows](../how-to/filter-rows.md)).
- **Parquet files are checked more strictly.** DuckDB 2.0 refuses some Parquet files that 1.5
  read: files with inconsistent decimal or time columns or value counts. Files Lakana wrote read
  as before. A file another program wrote (an external table's raw files, or a commit through the
  Iceberg REST catalog) can now fail the read with DuckDB's message: write that data again with
  a current version of the program, or ask the table's owner to overwrite it.
- **Lambdas need no change.** Write `x -> x + 1`, `lambda x: x + 1` or `LAMBDA x : x + 1`;
  Lakana hands DuckDB `LAMBDA x : …`, the only form DuckDB 2.0 accepts, so queries, views and SQL
  assets written with `->` keep running. A JSON arrow as a function argument
  (`upper(j -> '$.a')`) stays a JSON extract. Write `list_apply` for `apply` and `list_reduce` for
  `reduce`, which Lakana cannot read with a lambda.
- **`~` still matches the whole text** in Lakana (DuckDB 2.0 alone made it a partial match); no
  change.

**What a query may do**

- **A `PIVOT` lists its values.** A `PIVOT` without an `IN` list is refused with "A PIVOT needs
  the values of its ON column listed". Write `PIVOT ... ON a IN ('x', 'y') USING ...`.
- **Refused functions.** DuckDB's internal functions (any name starting with `__`), the sequence
  functions `setval`, `nextval` and `currval`, `index_key` and `to_aggregate_state` are refused,
  and `stats()` stays refused. So are the functions that run or plan SQL given as a value
  (`json_serialize_plan`, and `postgres_execute` and `mysql_execute`, next to the `query` functions
  refused before). The sequence functions and `to_aggregate_state` say what the call
  does and "Remove the call; a query reads tables only." Remove the call; for a `__` function,
  write the SQL it stands for, such as `CAST` or `BETWEEN`.
- **`CONNECT BY` is refused.** A hierarchical query with `START WITH ... CONNECT BY` runs as a
  recursive query, which a submitted query may not use. Write it without hierarchical recursion,
  for example with one join per level ([SQL in queries](../reference/sql-in-queries.md)).
- **A query opens only the tables it reads**, in local mode too: any other file or address is
  refused with "the query tried to open a file or an address that is not one of the tables it
  reads." A query that read a file by its path never worked through the guard; nothing to do.

**Certificates of the object store**

- **The worker checks its certificate authorities at start-up.** If `AWS_CA_BUNDLE`,
  `SSL_CERT_FILE` or the system bundle names a missing or unreadable file, a file without PEM
  certificates, or one that also holds a private key, the worker stops with a message naming the
  variable and the file. Point the variable at your object store's certificate authority, or
  unset it ([Network settings](../reference/network-settings.md#object-store-certificates-for-queries)).
- **A store whose certificate cannot be verified fails at once.** Such a query failed as a
  temporary storage error and was retried; now it fails at once, not retried, with a message that
  names the store's host and port, the bundle, `AWS_CA_BUNDLE` / `SSL_CERT_FILE` and the network
  settings. A store that worked before is not affected.

## Notes for 0.18.0

### How much disk an instance uses, and a package cache that trims itself

`lakana env usage [ENV]` (REST `GET /api/v1/environments/{name}/usage`, SDK
`Client.environments.usage`) shows how much storage each environment's tables, dropped tables,
artifacts and code archives use, and the size of each worker's caches; `lakana worker list` has
a column `caches`. See [Disk use](../how-to/instance-health.md#disk-use).

- **Migration `0048`** adds nullable columns for the sizes of tables, code archives and worker caches,
  empty on every row, and changes no data.
- **The figures fill in after the upgrade.** The worker's hourly sweep measures up to 200 tables
  each time, so on a large instance the first sweeps leave some tables `unmeasured`. Code archives
  deployed before this release keep no size and stay `unmeasured`.
- **The workers trim uv's package download cache** (`uv/` in the `worker-cache` volume) above
  `LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES`, 10 GiB by default. A cache that is larger after the
  upgrade is trimmed at the workers' first sweep; packages downloaded within
  `LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS` (24) stay. Set the limit to `0` to keep the old
  behaviour (never trimmed).
- **The usage needs `MANAGE_ENVIRONMENT`** on the environment; the workers' caches, as
  `lakana worker list`, on the instance.
- **New log codes:** `package_cache_trimmed` and `package_cache_over_limit` ([Log events](../reference/log-events.md)).

### CI deploys without a stored secret

A GitHub Actions or GitLab CI job can now deploy with its OIDC ID token instead of a stored Lakana
token: `lakana deploy` exchanges it when the job sets `LAKANA_OIDC=github` or `LAKANA_OIDC=gitlab`,
under a CI trust rule an administrator creates once. `lakana whoami` and `Client.whoami()` show
what a CI token may do (`ci_token`), and `Client.from_ci_oidc` does the exchange in Python. See
[Deploy from GitHub Actions without a stored secret](../how-to/deploy-from-github-actions.md) and
[Deploy from GitLab CI without a stored secret](../how-to/deploy-from-gitlab-ci.md).

- **Migration `0047`** adds the tables `ci_trust_rules`, `ci_tokens` and `ci_oidc_jtis`, all
  empty, and a nullable `ci_token_id` to `deploy_runs`; it changes no rows.
- **Existing CI keeps working.** A workflow or pipeline that reads a stored token
  (`LAKANA_TOKEN`) needs no change.
- **`lakana init` writes the CI files without a stored token** by default now. For files that read
  a stored token, as before, pass `--ci-auth pat`.
- **Setting both is refused.** A job that sets `LAKANA_TOKEN` and `LAKANA_OIDC` (or
  `LAKANA_OIDC_TOKEN`) stops with exit 3 and a message naming both: remove one.

### A readiness route, and worker healthchecks that ask the database

The server answers `/readyz` (no sign-in): `200` when the catalog database, the storage of each
environment and at least one worker answer, `503` with the names of those that do not. See
[The health routes](../how-to/instance-health.md#the-health-routes).

- **A bundle made before this release keeps its old healthchecks.** The new `compose.yaml` gives
  each worker the healthcheck `lakana-worker healthcheck`, which fails when the worker of the
  container has not written its heartbeat to the database in the last minute, and gives `server`
  its own healthcheck on `/healthz`. Copy both from a fresh `compose.yaml` ([Upgrading](../how-to/deploy-with-compose.md#upgrading)).
  The old worker healthcheck keeps working; it only checks less.
- **A table read on a storage that cannot be reached says so.** A query fails with
  `The storage of environment 'dev' could not be reached, so no table was read.` instead of
  `Resolving the tables this query reads failed (OSError).`, and the server answers `503` (try
  again) where it answered `500`.

### Stable codes on the log lines worth an alert

The log lines an operator may want an alert for end with `event=<code>`, for example
`event=memory_stop`; every code is in [Log events](../reference/log-events.md). The wording of
the lines is unchanged; only the code is added at the end of the first line. A rule that matched
a whole line up to its end no longer matches: match the code instead. New lines: the worker logs
when it takes and loses the scheduler role (`scheduler_role_taken`, `scheduler_role_lost`) and,
with the role, when it ends the runs of a lost worker (`worker_lost`); the server logs when
`/readyz` turns to failing and back.

### A new environment's root cannot lie in another environment's files

`lakana env create` (and `POST /api/v1/environments`) now refuses a storage root at or inside
another environment's `managed/`, `raw/`, `artifacts/` or `_artifacts/` directory, a deleted
environment's too, and a root whose own such directory would hold another environment's root.
There each environment keeps files the other cannot tell from its own, so the clean-up kept both
environments' leftovers for ever. The refusal (422) names the other environment and a root to use
instead. Environments may still share a root, and one may still lie elsewhere in another's (prod
at `s3://bucket`, dev at `s3://bucket/dev`).

- **Existing environments are not changed.** One that already lies in such a place keeps working,
  and `lakana env orphaned-locations` keeps all its directories as before; its `kept_because` now
  says to move the environment to a root of its own.
- **Scripts that create environments** at such a root fail at that step: give the environment a
  root of its own, such as the one the message names.

### The root of the disk cannot hold an environment

A new environment's storage root may no longer be the root of the disk, `file:///`, in any
spelling (`file://`, `file:////`, `file://localhost/`): the refusal (422) suggests a directory,
such as `file:///data/lakana`. Lakana's storage read such a root as each process's working
directory, so the files of an environment created earlier at `file:///` were written, read and
cleaned up relative to wherever the server and the workers were started, not at `/`.

- **An environment created earlier at `file:///`** is left as it is; the table-location clean-up and
  `lakana env orphaned-locations` keep its table directories, while the sweeps of unreferenced code
  archives and artifact files still run, in the worker's working directory. Create a new environment on a directory,
  move the tables there (deploy the projects to it, or load the data again), and then delete the
  old environment.

### A storage root must be in the environment's storage scheme

`POST /api/v1/environments` (and `Client.environments.create`) now refuses a storage root whose
scheme is not the environment's storage scheme: a `file` environment needs a `file://` root, an
`s3` one an `s3://` root and an `abfs` one an `abfs://` root. The refusal (422) names both schemes
and a root to use, such as `file:///data/lakana`. A root reaches the storage of its own scheme, not
the one the request named: `local://` is each process's working directory and `memory://` the
process's memory, and a `file://` root sent as `s3` was quietly a local directory. `s3a://`,
`s3n://` and `abfss://` count as `s3` and `abfs`; the message names the spelling to use, as before.
`lakana env create` is not affected: it takes the scheme from the root. An `s3` or `abfs` root
must also start with a bucket or container: `s3://`, `s3:///data` and `abfs:///x` are refused
(422) with the shape to use, such as `s3://bucket/lakana`, because the storage took the first
directory, or `managed/` itself, for the bucket. Existing environments on such a root are not
changed. A root may not contain `::` (an fsspec chain such as `file:///data/lake::memory:/x`),
which the storage read as only the part before it; an environment created earlier with one is
refused at every use: create it again on a plain root.

- **Existing environments are not changed.** One whose root has a scheme Lakana does not support
  (`local://`, `memory://`, `gs://` and the like) could never be used: every query, deploy and
  clean-up of it fails with `Unsupported storage scheme`, and nothing of it was written. Create a
  new environment under another name on a supported root, and delete the old one with
  `lakana env delete`. The deleted one keeps its name and stays in the database: its storage
  cannot be reached to purge it, so the clean-up logs that it skipped it on each run.
- **Scripts that create environments** with a root in another scheme fail at that step: send the
  scheme of the root, or a root in the scheme you meant.

### An allowed webhook host can no longer reach loopback or cloud metadata addresses

A host on `LAKANA_WEBHOOK__ALLOW_HOSTS` used to skip every address check of the worker's webhook
client. It still may use `http`, any port and a private address (`10.0.0.0/8` and the like), but a
name that resolves to a loopback, link-local or cloud metadata address (`127.0.0.1`, `::1`,
`169.254.169.254`, `fd00:ec2::254`, `100.100.100.200`, or an IPv6 address that carries one) is now
refused, so a listed name whose DNS changes cannot point the worker at them. The deprecated IPv6
site-local range `fec0::/10` is now treated as private everywhere: a webhook URL or a CI OIDC
issuer's host that resolves there needs its name in `LAKANA_WEBHOOK__ALLOW_HOSTS` or
`LAKANA_CI_OIDC__ALLOW_HOSTS`.

- **Who is affected.** Only an instance whose `LAKANA_WEBHOOK__ALLOW_HOSTS` lists a name that
  resolves to such an address. Its notifications fail with `the webhook host is a loopback,
  link-local or cloud metadata address, which is refused even for a host in
  LAKANA_WEBHOOK__ALLOW_HOSTS …`; point the name at the chat server's own address instead.
- **A receiver on the worker's own machine** keeps working when the setting lists `localhost`
  itself and the webhook URL names `localhost`: that entry, and only it, may reach loopback.

## Notes for 0.17.0

### Fewer sign-in events in the audit log

A successful sign-in (`AUTHENTICATION_SUCCEEDED`) used to be written for every request. It is now
written once per token every 15 minutes, with the number of sign-ins left out since the previous
event in `detail.suppressed`, so a screen that refreshes itself no longer fills the audit log. A
token's first use is always recorded, and failed sign-ins are recorded as before. If you count
sign-in events, for example in a report, add up `detail.suppressed` as well, or set
`LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES=0` on the server to write one event per request as before.

### The first administrator's token is written to a file, never to the log

When the server has to create its first administrator and no token was supplied
(`LAKANA_BOOTSTRAP_PAT_KEY_ID` unset), it used to print the new token in a WARNING line of its
log, where anyone who could read the log (`docker compose logs`, a log shipper) found a working
administrator's token. It now writes the token to a new file and logs only the administrator's
name, the token's key id and the file's path.

- **New setting `LAKANA_BOOTSTRAP_TOKEN_FILE`**, with no default: the server has no data directory
  of its own to put the file in. The file is created with mode 0600, readable by the server's user
  only, and never overwritten: if it exists (or is a symlink), the server refuses to start; read
  it, sign in, and **delete it after use**.
- **Who is affected.** An instance that starts the server itself, outside the Compose bundle and
  `lakana server start --local`, without `LAKANA_BOOTSTRAP_PAT_KEY_ID`, and has no administrator
  yet (a new instance, or the recovery in [step 5 of "Delegation needs the ADMIN
  role"](#delegation-needs-the-admin-role)). It stops at start-up with a message that names the setting: set
  `LAKANA_BOOTSTRAP_TOKEN_FILE` to a path in a directory only the server's user can enter, or
  supply `LAKANA_BOOTSTRAP_PAT_KEY_ID` and `LAKANA_BOOTSTRAP_PAT_SECRET`. A running instance
  that already has an administrator is not affected. The Compose bundle and a local instance are
  not affected either: both supply the token themselves, and a local instance's later run that has
  to mint one writes `bootstrap-token` in its data directory.
- **A key id that is already taken now stops the start-up.** Before, the server made up a
  different token and logged it. Now it refuses and says so: use a key id never used before (a
  revoked token keeps its key id), or unset the key id and set `LAKANA_BOOTSTRAP_TOKEN_FILE`.
  Only the recovery with every administrator revoked reaches this; see the recovery steps below.

### Table locations are recorded, and a create that never finished is cleaned up

Migration `0042` adds the table `table_locations`: one record for the storage directory of each
managed table. The migration writes a record for every managed table that exists, with the
directory its metadata file lies in; external tables, views and artifacts get none. **Nothing is
deleted by the upgrade.**

From now on a create records its table's directory before it writes the first file. When a create
fails and Lakana cannot tell whether its commit landed, the files stay as before, and the error now
says that the worker's sweep removes them once it has proven that the commit did not land: after
the run that made the create has ended, or, for a create outside a run, after
`LAKANA_TABLES__ABANDONED_GRACE_HOURS` (new, 24 by default). A create known to have failed removes
its files at once, as before; one that lost a race for its name now removes its metadata file too.
The sweep runs with the others every `LAKANA_RUN__SWEEP_INTERVAL` seconds and never removes a table
that exists or a directory that has no record: directories that an earlier release left behind
(from a failed create, or from a drop) are only counted and reported in the worker's log, as
`<n> table directories under managed/ in the storage of environment <env> have no location record;
they are left alone.` `lakana env orphaned-locations` removes them on request (below). The audit log gains
`TABLE_LOCATION_ABANDONED` and `TABLE_PURGED`; see [The sweep](../how-to/deploy-with-compose.md#the-sweep).
Going back to the previous release drops the records and keeps every file.

### A dropped table's files are deleted after an undo period

**This changes what a drop does.** Until now a drop removed the table from the catalog and kept its
files for ever. From this release a drop of a managed table keeps its files for the undo period
(`LAKANA_TABLES__UNDO_DAYS`, new, 7 days by default), and then the worker's sweep deletes them: no
earlier than the end of the period, and no later than one sweep after it. Within the period
`lakana table undrop NAMESPACE.TABLE` restores the table with its data and with exactly the grants,
row filters and column masks the drop took away; `lakana table list NAMESPACE --dropped` shows what
can be restored and until when. `lakana table delete --purge`, the REST `?purge=true` and the Iceberg
REST `purgeRequested=true` make the files due at the next sweep, and no longer delete the current
metadata file in the request itself. An external table's files are never deleted. Every drop now
writes a `TABLE_DROPPED` audit event, and a restore `TABLE_UNDROPPED`. See
[Drop and restore a table](../how-to/drop-and-restore-a-table.md).

To keep a dropped table's data, restore it within the undo period, or set
`LAKANA_TABLES__UNDO_DAYS` higher on the server (up to 365) **before** you drop it: each drop
records the end of its own period. Migration `0043` adds two columns to `table_locations`; it
deletes nothing.

Tables dropped before this release have no undo period and are not deleted: their directories stay
and are counted in the worker's log as directories without a record; `lakana env orphaned-locations`
removes them on request (below). Going back to the previous release drops the records, as above, and keeps
every file; a table dropped in the meantime can no longer be restored.

### Tables can expire, and nothing does unless you say so

A table can now have a retention: `lakana table retention NAMESPACE.TABLE --days N` (or `--on` a
day, or `--never`), `--expire-after N` on `lakana table create` and on `lakana sql --into ... --mode
create`, and a retention for every table of an environment that sets none, `lakana env retention
ENV --days N`. The worker's sweep then drops a table once its retention has passed, as a drop with
an undo period of at least a day, so `lakana table undrop` restores it. Protected environments and
the tables a deployed project builds never expire. See [Let tables expire](../how-to/let-tables-expire.md).

**Nothing expires because of the upgrade.** Migration `0044` adds the retention columns to
`tables` and `environments`, empty on every row, which means no retention; it changes and deletes
nothing. Three settings are new: `LAKANA_TABLES__EXPIRY_WARNING_DAYS` (server, 7),
`LAKANA_TABLES__MAX_EXPIRIES_PER_SWEEP` (workers, 100), and the workers now read
`LAKANA_TABLES__UNDO_DAYS` too, so set it for the server and the workers alike if you changed it.
Going back to the previous release drops the columns: every retention is forgotten, and a table that
expired meanwhile can still be restored within its undo period.

### Deleting an environment deletes its tables' files after an undo period

**This changes what deleting an environment does.** Until now `lakana env delete` removed the
environment and everything in it from the catalog and kept every file for ever. From this release
it drops every managed table of the environment as `lakana table delete` drops one: the files stay
for the undo period (`LAKANA_TABLES__UNDO_DAYS`, 7 days by default), and then the worker's sweep
deletes them, no earlier than the end of the period and no later than one sweep after it. Until
then `lakana env restore ENV` brings the environment back with its tables, their data and the
grants, row filters and column masks the deletion took away, and `lakana env list --deleted` shows
until when. The environment's name stays taken meanwhile: creating an environment of that name is
refused until its files are deleted. `lakana env delete ENV --purge` makes the files due at the next
sweep; the sweep then also removes the environment. An external table's files are never deleted.
See [Delete and restore an environment](../how-to/drop-and-restore-a-table.md#delete-and-restore-an-environment).

- **To keep the data** of an environment you delete, restore it within the undo period, or set
  `LAKANA_TABLES__UNDO_DAYS` higher on the server and the workers **before** you delete it.
- **REST.** `DELETE /api/v1/environments/{name}` now answers 200 with the environment and its
  `deleted_at` and `purge_after` instead of 204 without a body, and takes `?purge=true`. New:
  `POST /api/v1/environments/{name}/restore` and `GET /api/v1/environments?deleted=true`.
  `EnvironmentOut` gains `deleted_at` and `purge_after`. The SDK's `environments.delete` returns the
  environment (`None` from an older server), and gains `purge`; `environments.restore` and
  `environments.list(deleted=True)` are new.
- **Who restores.** Deleting an environment revokes the grants given on it, so an environment admin
  who deleted one cannot restore it; an administrator of the instance can, and the restore gives
  those grants back.
- **Environments deleted before this release** left no trace in the catalog and kept every file;
  nothing changes for them. Their directories are counted in the worker's log as directories
  without a record; `lakana env orphaned-locations` removes them on request (below).

Migration `0045` adds five columns to `environments`, empty on every row: no environment is deleted
by the upgrade. Going back to the previous release drops the columns: an environment deleted
meanwhile shows up again without its tables, each of which stays a dropped table that `lakana table
undrop` restores within its undo period, as before.

### A storage root must be written the one way locations are compared

`lakana env create` (REST `POST /api/v1/environments`, SDK `environments.create`) now refuses a
storage root written in another spelling of a place, with 422 and a message that names the
spelling to use: an upper-case or alias scheme (`FILE://`, `s3a://`), a doubled slash anywhere
(`file:////data`, `s3://bucket//lake`), a `.` or `..` segment, a percent escape, or `*`, `?`, `[`,
`]`. One trailing slash is still accepted. Two spellings of one directory hide each other's files
from the checks that keep the clean-up from deleting what another environment uses.

Environments that already have such a root keep it. The clean-up never deletes files near one:
see [Clean up files left by older releases](../how-to/clean-up-files-left-by-older-releases.md).
**A location that starts with a doubled slash after the scheme (`file:////data/...`) no longer
counts as canonical**, as other doubled slashes did not already: the tables of an environment whose
root starts that way can no longer be read or maintained. Create a new environment with the
canonical root (`file:///data/...`; the same directory on a local disk) and load its tables
there again.

**A `file` root must be an absolute path with no host**: `file:///data/lake`, not `file://data/lake`
(read against each process's working directory) or `file://localhost/data/lake` (refused with
`file:///data/lake` as the spelling to use). Locations of that kind no longer count as canonical
either, so the tables of an existing environment with such a root can no longer be read; create one
with an absolute root and load them there again. The clean-up keeps every file on the local disk
while such an environment exists: delete it.

**A new table or namespace name may not contain `%`** (a storage may read `%41` as `A`, so two
names would share one directory); every other character is allowed except `.`, `/`, the unit
separator and a NUL byte. Existing names with `%` stay readable, listable, droppable and renamable
away, as dotted names do, and a project whose assets or checks name such a table still deploys.
Other names (jobs, checks, repositories, package indexes, artifacts) may still contain `%`. The clean-up compares their locations both as written and decoded, so
such a table keeps only the directory it names.

### Remove the table directories older releases left behind

Directories that earlier releases left under `managed/` without a record (tables dropped before
this release, creates that failed, environments deleted before this release) are kept by the
upgrade and by the sweep, and only counted in the worker's log. **Nothing is deleted unless you
ask.** When you want the space back, `lakana env orphaned-locations ENV` lists them for an
environment's storage root without deleting anything, and `lakana env orphaned-locations ENV
--delete` removes those whose newest file is older than `--older-than` (30 days by default), each
checked again under the sweep's lock first. It needs the admin role on the whole instance, accepts
a deleted environment, and records `ORPHANED_LOCATIONS_DELETED` in the audit log. REST: `GET
/api/v1/environments/{name}/orphaned-locations` and `POST
/api/v1/environments/{name}/orphaned-locations/delete`; SDK: `environments.orphaned_locations` and
`environments.delete_orphaned_locations`. If you restored the catalog from a backup, keep
`--older-than` above the backup's age: a table created after the backup is listed too. See
[Clean up files left by older releases](../how-to/clean-up-files-left-by-older-releases.md).

### Administrators no longer see other people's SQL text or column values in the audit log

**This changes what an administrator sees.** Until now everyone who could read the audit log
(`READ_AUDIT_LOG`, which the `ADMIN` role holds) saw the SQL text of every completed query in it.
From this release query text and column values in **someone else's** events need a new right,
`READ_QUERY_TEXT`, which no role holds, `ADMIN` included: the SQL text of a query
(`detail.sql_text` of `QUERY_COMPLETED`) and the watermark of an incremental write
(`detail.watermark` of `TABLE_WRITE`, the highest value of its key column). Without it
`sql_text` shows `{"sha256": ..., "length": ...}` of the text, and `watermark` shows
`{"hidden": true}`, with no hash: its values are few enough to guess from one. Your own events still show in full, and nothing
else in the log changes.

- **To see the text again**, an administrator grants the right by name, on the instance:
  `lakana grant create --kind INSTANCE --principal NAME --action READ_QUERY_TEXT`. The grant is
  in the audit log like every grant. A script that reads `sql_text` or `watermark` of other
  people's events through the SDK or REST needs its principal to hold it.
- **Reading the audit log is recorded.** A page of `lakana audit list` that shows other people's
  events, or a filter on someone else (`--principal-id`), writes one `ACTIVITY_VIEWED` event (view
  `audit_log`) per person and hour, like the query list of a manager. If that event cannot be
  written, the read is refused with 503. `LAKANA_VIEW_AUDIT__ENABLED=false` turns it off.
- **New setting `LAKANA_AUDIT__QUERY_TEXT`**, on the server and every worker: `full` (the default,
  as before), `hash` (only the SHA-256 and length) or `off` (no text). It applies to new events;
  events already written are not rewritten. An unknown value stops the start with a message that
  names the setting.

Migration `0046` adds an index to the audit log and changes no rows. Going back to the previous release shows the text to every reader of the log again;
events written with `hash` or `off` stay without it.

## Notes for 0.16.0

### Grants, principals and groups are listed in pages

`GET /api/v1/grants` takes every filter as optional now. Without one it returns every grant, where it
returned only the instance grants, 100 per page by default (`limit`, at most 200). The next page's
cursor is in the response header `X-Next-Cursor`, which is absent on the last page; the body stays a
JSON array. To list only the instance grants, keep the grants that have no environment. The principal
and group lists are paged the same way and come newest first (they had no set order before); `?q=` finds a name.
A script that reads one of these lists in one request reads only its first page now: pass `limit` and
follow `X-Next-Cursor`, or use the SDK's `iter()`. `lakana grant list`, `lakana principal list` and
`lakana group list` print a `More results: … --cursor X` line when there is more. See
[Access](../reference/access.md).

### A query that is passed by says so

Migration `0041` adds a nullable `query_passed_by_until` to `principals`; every row stays empty. When a
worker waits too long for the lock that serialises the start of one user's queries, it now writes
the time (30 seconds ahead) until which workers pass that user's ad-hoc queries by, and the query's
wait reason is the new `lock_timeout` instead of `starting`
([Why is it waiting?](../how-to/why-is-it-waiting.md)). Nothing is removed. Going back to the
previous release drops the column; that release keeps this in its workers' memory only.

### Every task failure has a code and a next step

Migration `0040` adds a nullable `failure_code` to `task_runs` and `task_attempts`. The rows that
exist stay as they are: a task that failed before the upgrade has no code, and the API shows it as
`unknown` (a display value that is never stored), with `error_message` unchanged. Every failure
after the upgrade stores one of the codes in [Why did it fail?](../reference/why-did-it-fail.md).
New: `failure` (`code`, `cause`, `next_step`) on each task and each attempt of the run routes, and
`failed_tasks` on a run; `lakana runs show` prints the code and the next step, `lakana run` prints
the next step of a task that fails. Nothing is removed: `error_message` stays as the detail. Going
back to the previous release drops the codes. A worker that is older than the server stores none,
so upgrade the workers together with the server.

### Search across tables, columns, runs and people

Migration `0039` adds the table `table_columns`, which search reads to find columns. It starts empty
and is written from now on in the transaction of every schema change (a created table, a commit, an
external table, a view). Tables that exist already get their columns from the worker's housekeeping
sweep, 50 tables per pass and one pass every `LAKANA_RUN__SWEEP_INTERVAL` seconds (3,600 by
default): 1,000 tables take about 20 hours, and a managed table's columns are read from its metadata
file in object storage. Until a table is filled in, search finds it by its name and description but
not by its columns. With `LAKANA_RUN__SWEEP_INTERVAL=0` the sweep never runs; a schema change fills
in that table. New: `GET /api/v1/environments/{env}/search`, `lakana search` and `client.search`;
nothing is removed. See [Search](../reference/search.md) and [Find a table](../how-to/find-a-table.md).
Going back to the previous release drops the stored columns.

### Table health: the numbers of a table, and who reads it

Migration `0038` adds the table `table_reads`. It starts empty: the worker counts the reads of the
queries that complete from now on, so `lakana table health` shows readers only for queries run after
the upgrade. Reads are kept for 90 days and shown over 30 by default; an administrator changes both
with `LAKANA_TABLE_READS__KEEP_DAYS` and `LAKANA_TABLE_READS__DEFAULT_DAYS` (the server and every
worker read them). The JSON of a table gains `last_write_at`, `row_count`, `size_bytes` and
`file_count`; nothing is removed. A new route and command show the rest; see
[Table health](../reference/table-health.md). A Python task reads its inputs through the query
API, so its reads count too. Going back to the previous release drops the counts.

### Tables and columns have descriptions

Migration `0037` adds the two texts of a description to `tables` and a table `column_descriptions`.
Nothing is shown until the next deploy: it publishes each asset's `description:` and `columns:` onto
the tables that already exist. A description set with `lakana table describe` is shown instead of the
project's text and a deploy never replaces it; see [Describe a table](../how-to/describe-a-table.md).
Going back to the previous release drops the descriptions, edits included. The JSON of a table
and of each column gains `description` and `description_source`; nothing is removed. A table created
through the API now records its creator as its owner, which is what lets the creator describe it.

### Row filters and column masks are type-checked when they are created

A row filter or column mask is now checked against the table's columns, as they are when you
create it, and refused if it would need Lakana to convert a value: a text column compared with a
number (`code = 5`), two columns of different types, a column that is not boolean used as a
condition, a comparison with `NULL`, or a mask whose type differs from its column's (`0` on a text
column). The message names the column, both types and what to write, for example "`code` is text;
compare it with '5', not 5." A comparison of numbers is accepted only when the engine's conversion
cannot fail: a literal must fit the column exactly (an integer column takes whole numbers in its
range, `decimal(p,s)` at most `s` decimal places and `p - s` digits, `float` and `double` finite
numbers), and two columns must be both integers, both `float`/`double`, or decimals of one
precision and scale. A `timestamptz` column compares only with another `timestamptz` column. A
date or timestamp is written as a typed literal (`DATE '2026-01-01'`), which is new and checked at
creation. A column mask is now cast to its column's declared type when it is read, so a reader gets
the column's own type (a `0` on a `long` column is a `BIGINT`), and a mask is accepted only when
every value fits; a value that stops fitting later (the column the mask reads was widened) reads
as `NULL`. The check and the cast apply to Iceberg tables and to external tables of Parquet files (checked
against the types stored for the table: `int64`, `date32[day]`, `decimal128(10, 2)`,
`large_string`, ...). A Parquet timestamp in seconds, milliseconds or microseconds is a `timestamp`; a nanosecond one compares only with a literal inside 1677-09-21 to 2262-04-11 and its masks are not type-checked. Filters and masks on external tables of CSV or JSON files are checked against the
SQL subset only, as before, and their masks are stored as not type-checked, because the types stored
for such a table are only a guess from one sample file. See [Show some people
only some rows](../how-to/filter-rows.md#an-expression-must-not-fail-on-any-stored-value) and
[Mask a column for some people](../how-to/mask-a-column.md#an-expression-must-not-fail-on-any-stored-value).

Filters and masks that already exist are **not** checked again and are **unchanged**. Migration
`0036` marks every existing mask as not type-checked, and a read gives its value exactly as it
did before; only a mask created from this release on is checked and cast to its column's type. To
get the check and the type-keeping cast for an existing mask, create it again with the same command
(`lakana column-mask create ...`) and then revoke the old one. A filter or mask that compares
across types, or a mask of another type than its column, still fails every query it covers or
gives the reader another type, as it did before; the failing ones show the message "The query
failed on a value it could not convert or compute, under a row filter or column mask on ...". To
find them, as an administrator:

1. List them: `lakana row-filter list` and `lakana column-mask list` (`--env`, `--principal` or
   `--group` narrow the list). Each row shows the table and the `expression`, and for a mask the
   column.
2. Read the columns and their types of each table named: `lakana table get NAMESPACE.TABLE`.
3. Look for a comparison (`=`, `<>`, `<`, `<=`, `>`, `>=`) of a text column with a number or of
   two columns of different types, for a column that stands alone as a condition and is not
   boolean, for `-column`, for a comparison with `NULL`, and for a mask whose value is of another
   type than its column (a number on a text column, text on a number column, `0` on a `date`).
4. Create the corrected filter or mask, then revoke the old one (the order matters: until the new
   one exists, revoking the old one shows the real values).

A script or a deploy that creates a filter or mask with such an expression now fails at the
creation, with the message above; fix the expression there.

### Errors on reads under a row filter or column mask

Errors on reads under a row filter or column mask always show the fixed message, whatever the class of the database's error. Binder, catalog and parser errors, which used to keep their text, now show it too, and so do storage, network and memory errors (they keep their kind, so a retry and the memory handling are unchanged), the wording for a file that stores a column in another type, and the diagnosis of a view that no longer plans. A database out-of-memory error shows the fixed message followed by the advice to read less or raise the memory limit. Cancellations, time limits, the memory stop of the worker and a query process ended from outside keep Lakana's own wording. A misspelled column or function in your own query on such a table is told the same way: run the query against a table that has no filter or mask, or check the names with `lakana table get`.

## Notes for 0.15.0

### A database check on a secret's use

Migration `0035` runs automatically (see step 3 of [What every upgrade
asks](#what-every-upgrade-asks)). It adds a check to the `secrets` table: a secret is unbound,
bound to its own environment, or bound to a package index or repository with that entry's id, and
nothing else. Lakana never wrote anything else, so no secret should break it. If one does (a
secret edited by hand in the database), the migration stops before it changes anything and names
the secret ids; bind each of them again with `lakana secret bind` on the release you are
upgrading from, and run the upgrade again. The migration never rewrites a binding itself.

A change of an index's or a repository's URL or credential that races its removal now answers
"not found" (404) in local mode, as it already did elsewhere, instead of a server error.

### Changing storage options needs the right to manage secrets

While an environment has a storage secret, `lakana env set-storage --option` (the storage options,
such as `s3.endpoint`) now also needs `MANAGE_SECRET` in that environment, as binding a secret
does; before, `MANAGE_ENVIRONMENT` was enough, and the secret could be sent to another endpoint.
This affects someone who holds `MANAGE_ENVIRONMENT` but not `MANAGE_SECRET` (the `ADMIN` role has
both): the command is now refused and audited, and says who can run it. Environments without a
storage secret, and options sent unchanged, behave as before.

In the Compose bundle, the set-up attaches the SeaweedFS secret to `dev` only together with the
bundle's own storage options, in one step. A `dev` at the bundle's storage root that has no
storage secret therefore gets its storage options set to the bundle's when the stack starts, and
the set-up prints a line saying so. Change them again afterwards with `lakana env set-storage`
(which then needs `MANAGE_SECRET`, see above).

### A commit's manifests are limited to 64 MiB

No migration. The check that every data file of an Iceberg REST catalog commit has field ids
reads the commit's manifest list and manifests whole. It now refuses a commit one of whose
manifest files is larger than 64 MiB, before reading it, and the reading counts towards the same
60 seconds as the footers
([the refusal](../reference/checks.md#when-a-commit-through-the-iceberg-rest-catalog-is-refused)).
Split such a commit into smaller ones, so that each manifest lists fewer data files; a manifest of
64 MiB lists far more than the 2,000 files a commit may add. The database connection is also given
back while the files are checked, so several large commits no longer hold up other writers.

## Notes for 0.14.0

### The project moved to the lakana-labs organisation

The repository is now `https://github.com/lakana-labs/lakana`, and new images are published as
`ghcr.io/lakana-labs/lakana`. Old links to `github.com/chrschy/lakana` still lead to the new place.
The old images under `ghcr.io/chrschy/lakana` stay where they are but get no new versions.

A Compose bundle that an earlier `lakana bundle init` wrote names the old image in its `.env`
(`LAKANA_IMAGE=ghcr.io/chrschy/lakana:<version>`). Before you pull, change it to
`LAKANA_IMAGE=ghcr.io/lakana-labs/lakana:<new version>` and upgrade as in
[Upgrading](../how-to/deploy-with-compose.md#upgrading). A bundle
that this release writes already names the new image.

### Compose bundle: SeaweedFS moves behind an S3 gateway

The bundle's `compose.yaml` now runs SeaweedFS as two containers, `seaweedfs-core` and the S3
gateway `seaweedfs`, on separate networks, so the worker cannot reach SeaweedFS's unauthenticated
internal HTTP APIs (#14). The endpoint `http://seaweedfs:8333` and the S3 keys do not change. An
existing deployment keeps its old file until you replace it:

1. Get the new `compose.yaml` the way [Upgrading](../how-to/deploy-with-compose.md#upgrading)
   describes (`lakana --profile upgrade-check bundle init ...`, then
   `lakana profile remove upgrade-check --yes`). Replace your `compose.yaml` with the new one and
   carry over any lines you added, usually under `x-lakana-env`. Keep your `.env`.
2. `docker compose down`. The volumes stay. This matters: the old `seaweedfs` is a full
   `weed server` on the `seaweed` volume, and it must stop before `seaweedfs-core` starts on the same volume.
3. `docker compose up -d --wait`.

Your data stays. The volumes (`pgdata`, `seaweed`, `worker-cache`) keep their names and
contents; `seaweedfs-core` mounts the same `seaweed` volume. Expect a minute of downtime while
SeaweedFS restarts.

### A secret is bound to one use

Migration `0034` runs automatically (see step 3 of [What every upgrade
asks](#what-every-upgrade-asks)). A secret now has one use: a package index, a
repository, or its own environment (its storage and its webhooks), and Lakana
refuses it for anything else. Only someone who may manage secrets (the `ADMIN` role) binds a
secret. A person who may only register indexes or repositories can name a secret that is already
bound to that same entry, and nothing else.

The migration binds a secret only when exactly one package index or repository names it and its
environment does not use it for storage or for a webhook (delivered, or in any deployed project
version). It leaves unbound every secret that its environment uses and every secret that several
entries name. The only webhook it cannot see is one in a `lakana.yaml` that was never deployed,
and such a webhook never runs.

An unbound secret keeps serving its environment's storage and webhooks. An index or a
repository that names it is refused until someone who manages secrets binds it, and the message
gives the command. A secret that is both a webhook secret and an index credential stays unbound:
bind it to its use, or give each its own secret.

**One secret per entry.** A secret serves one index or repository. Where several indexes or
repositories shared one credential (one token named by several repositories, say), create one
secret per entry (the same value is fine) and point each entry at its own:
`lakana secret create NEW --env ENV --repository REPO --values-stdin`, then
`lakana repo update REPO --credential-secret NEW` (`--index INDEX` and `lakana index update
INDEX` for an index). Until then every entry but one is refused, and the message gives these
two commands.

A secret stays unbound when several entries name it, because then its use is unknown, or when
its environment uses it, because binding it to an index would stop it serving storage or
webhooks. After you upgrade, an administrator lists the secrets of each environment and binds
what shows no use. Check the URLs in `lakana index list` and `lakana repo list` first, because
binding approves the URL the entry has at that moment (the command prints it, and the audit log
keeps it). Then bind credentials of an index or a repository with `--index` or `--repository`;
storage and webhook secrets with `--own-env`, so that nobody's naming can claim them. A secret
bound to an index stops serving storage and webhooks, so bind a secret to an index only when
it is that index's credential.

<!-- not tested: needs an upgraded instance, and ENV, NAME, INDEX and REPO are placeholders -->
```bash
lakana secret list --env ENV
lakana index list
lakana repo list
lakana secret bind NAME --env ENV --index INDEX
lakana secret bind NAME --env ENV --repository REPO
lakana secret bind NAME --env ENV --own-env
```

A new URL for an index or a repository with a bound secret needs the right to manage secrets in
that secret's environment, because the URL is where the secret goes.

## Notes for 0.13.0

### Iceberg tables are read by field id

Migration `0033` runs automatically (see below). Lakana reads the data files of an Iceberg table by the field id of each column, as
the Iceberg specification requires. After a schema change, an added column reads as NULL in older
rows, a renamed column keeps its data, a column dropped and added again under its name is a new
column (a type change through `evolve_schema` is one too: its older rows read as NULL), and a
widened column reads in its new type. Queries, row filters and column masks that
failed after such a change (``The data files of `TABLE` do not all have the column ...``) now
run; that message remains only for raw-zone (external) tables. A field inside a struct, list or
map column is read by name: after one was renamed, or dropped and added again, reads of the
table are refused with ``'TABLE' cannot be read: the field ...`` until its rows are written
again. A table with a column of a type Lakana cannot read yet (nanosecond timestamps, `unknown`, the
geospatial types of format version 3) is refused as a whole: ``'TABLE' has the column 'x' of type
..., which Lakana cannot read yet.`` Store the column as another type. A filter on a float column
widened to double still needs a range
([Show some people only some rows](../how-to/filter-rows.md#columns-of-a-table-made-outside-lakana)).

Row filters and column masks are bound to a column name, while a table is read by field id, so a
rename could leave a policy on another column. From this release a read is refused, and nothing is
shown, when a column was renamed or replaced (dropped and added again, also by a type change
through `evolve_schema`) since the policy was created, and the old or the new name is one the
policy uses:
`` `TABLE` has a row filter or column mask that uses column `COLUMN`, and a column was renamed or replaced since the policy was created ...``
Each policy now records the table schema it was created against. A policy of an earlier release
records none and is judged against every schema the table has had, so after the upgrade a table whose
policy column was renamed or replaced at any time is refused until an administrator creates the
policy again for the column it is meant for and revokes the old one. Before you upgrade, list the
row filters and column masks (the `list` subcommand of `lakana row-filter` and of
`lakana column-mask`) and look for tables whose columns were renamed or retyped since the policy
was made. The order after a rename is: rename, create the policy for the new name, revoke the old
one. Reads are refused until the old one is revoked, and nothing shows in clear at any step
([Mask a column](../how-to/mask-a-column.md#after-a-rename),
[Show some people only some rows](../how-to/filter-rows.md#after-a-rename)). Migration `0033` adds
a column `schema_id` to the tables of row filters and column masks.

From this release on, a commit through the Iceberg REST catalog that adds a data file without
field ids, for example one registered with pyiceberg's `add_files`, is refused and nothing is
applied ([the refusal](../reference/checks.md#when-a-commit-through-the-iceberg-rest-catalog-is-refused)).
Write the files with a client that sets field ids: pyiceberg's `append` and `overwrite` do, and
Lakana's own writes always do. A commit may add at most 2,000 data files, and the check has 60
seconds; split a larger load into several commits. Files that other Iceberg clients committed
before this release are not checked. If such a file lacks field ids, every column of it reads
as NULL, with no error. Ask the owner of the table to write its rows again, with Lakana
(`replace`) or with a client that writes field ids. Field ids that match no column of the table
read as NULL as well.

### A query that changes data is refused wherever the statement sits

No migration. A query, an asset, a check or a view whose SQL holds `INSERT`, `UPDATE`,
`DELETE`, `MERGE`, `COPY`, `CREATE`, `DROP`, `ATTACH`, `SET` or another statement that is not a
read is refused also when the statement sits inside a CTE or a subquery:
``A query reads data and cannot change it: `DELETE` is not allowed, also not inside a CTE or
subquery.`` DuckDB 1.5 rejects these shapes itself; the next major DuckDB release runs them, so
Lakana refuses them first. A query may not call `getvariable` either.

`DESCRIBE`, `SUMMARIZE`, `SHOW` and `SELECT ... INTO` are refused in a query too, also inside
a subquery or a CTE (`SELECT * FROM (SUMMARIZE t)` no longer works). `DESCRIBE`, `SUMMARIZE`
and `SHOW` were never allowed as the whole statement; `SELECT ... INTO` is now refused there
too. To get the numbers, use an aggregate query (`SELECT count(*), min(x), max(x) FROM t`) and
read the column list from the result.

### Errors under a row filter or column mask

No migration. A query that reads a table under a row filter or column mask no longer shows the
database's text for a failure, except binder, catalog and parser errors: the run's error, and a check's error, hold one fixed message that names
the table and does not show the value. Cancellations, time limits and memory errors, and errors on a table with no filter or mask, keep their text.

## Notes for 0.12.1

### Columns can be qualified with the table's name

No migration. `SELECT orders.id FROM gold.orders`, `gold.orders.id` and `o.id` after
`FROM gold.orders AS o` now work, also under a row filter or column mask; before, they failed with a
message about a read called `__lakana_...`. The table's own name serves as the alias only when no
other table, alias, CTE, derived table or view of the query has that name. The full name
(`gold.orders.id`) works in the level of the query that reads the table; inside a subquery, a CTE,
a derived table or a `LATERAL` join write `orders.id` or give the table an alias. When two
tables share a name (`gold.orders JOIN silver.orders`, or a subquery over `silver.orders`
inside a query over `gold.orders`), nothing changes: the query runs as before, and a column qualified with that name
fails in the database as before. Give each table an alias.

## Notes for 0.12.0

This release changed a lot: who may hand out access, which package sources a project may
use, how a query runs, what happens to a query or a deploy whose worker is gone, which work a
worker takes and how many ad-hoc queries one user runs at once, how snapshots are expired, and
that a project can state in SQL what must be true of its tables. Read every part below. Run the
migrations as always: `0020` is new, it creates the table of registered
package indexes and widens the `action` column of the grants table; `0021` adds one column to the
table of query runs; `0022` adds the table of workers, a column to the query runs and to the
deploy runs, and two indexes; `0023` adds one column to the table of workers; `0024` adds the table of check results and
a few columns to the tables, query runs, task runs and deploy runs; `0025` adds three columns to the table of tables
(the mark of a table that keeps data after a failed check); `0026` adds one column to the table of query runs
(the check result whose rows a query shows).

### Snapshots are expired with `lakana table maintain`

**This is a breaking change for Iceberg clients.** A commit through the Iceberg REST catalog can no
longer remove snapshots, remove schemas or partition specs, or remove or set a ref named
`lakana-check-failed-*`. It is refused with 403 for every caller, an administrator included, and a
PyIceberg job, or one from another engine, that expires snapshots now gets that 403. It also cannot assign another UUID
to a table (422). Expiring snapshots was possible for anyone who could write a table, and it could take the
snapshot that a running task or a kept tag still needs. If a job from another engine or a PyIceberg script
expires snapshots on a Lakana table, change it to run `lakana table maintain` instead (see
[`lakana table maintain`](../reference/checks.md#lakana-table-maintain)). Removing a branch or a
tag of your own is unchanged.

Two more changes of the same kind. A caller without `READ_TABLE` can no longer make an earlier
schema, partition spec or sort order of a table current; it can still add a new one and make that
one current. **A task token that no task owns (one made by hand over REST) can no longer run
`lakana check rows`**, and it is refused the read of a table kept after a failed check, like a
pipeline task: read with your own token. See [When a commit through the Iceberg REST
catalog is refused](../reference/checks.md#when-a-commit-through-the-iceberg-rest-catalog-is-refused).

### A deployed snapshot is a project version

<!-- old-name -->

What a deploy records for one commit in one environment is now called a *project version*. It was
called a job version. The word *job* is now free for a named entry in `lakana.yaml` that you can
start by hand, through the API or on a schedule. Nothing is migrated, and the database tables keep
their names. The old names are gone with no alias, so change these where you use them:

| Where | Before | Now |
|---|---|---|
| Command line | `lakana job-version show ID` | `lakana project-version show ID` |
| REST route | `GET /api/v1/environments/{env}/job-versions/{id}` | `GET /api/v1/environments/{env}/project-versions/{id}` |
| Python SDK | `client.job_versions.get(...)`, which returns `JobVersion` | `client.project_versions.get(...)`, which returns `ProjectVersion` |
| JSON field | `job_version_id` | `project_version_id` |
| JSON field | `rollback_to_job_version_id` | `rollback_to_project_version_id` |

The JSON fields are in the answers of `deploys`, `deployments`, runs and artifact versions, and in
the SDK objects built from them. The `detail` of the audit events `RUN_STARTED`, `DEPLOYED`,
`DEPLOY_DRY_RUN`, `ROLLED_BACK`, `UNDEPLOYED` and `CODE_INTEGRITY_FAILED` uses
`project_version_id` for events written from this release on. Events written earlier keep the old
key (`job_version_id`), so a script that reads both has to accept both.

<!-- /old-name -->

### Jobs and the triggers that start them

A project can declare **jobs** in `lakana.yaml` and give them a cron schedule in a time zone; a
job also starts by hand or through the API. Nothing is required of a project that declares none.
See [Run a job on a schedule](../how-to/run-a-job-on-a-schedule.md).

- **Migration `0028`** adds columns to `runs` and to `deploy_runs` and two tables, `job_schedules`
  and `job_fires`. Run the migrations as always. A project version that declares jobs has manifest
  version 5. The packages now depend on `tzdata`, the one source of time zone rules, so a slim
  container needs no system time zone database.
- **A schedule needs a run principal.** After the first deploy that declares a schedule, bind a
  service account with `lakana deployments set-run-as`, or the job shows `no run principal` and
  does not run. The deploy warns about it.
- **A project with jobs needs workers of this release.** Upgrade every worker before the first
  deploy that declares `jobs:`. A worker of the earlier release refuses the runs of such a project
  version (manifest version 5), and while a worker of the earlier release leads the scheduler it
  neither fires schedules nor starts waiting starts, although the new server accepts the start
  and answers `202`.
- **New REST routes**, all under `/api/v1/environments/{env}`:
  `GET /jobs`, `POST /jobs/{job}/runs` (201 with the run, 202 with the waiting start),
  `POST /jobs/{job}/pause` and `POST /jobs/{job}/resume`. Starting needs `RUN`; pausing and
  resuming need `DEPLOY` in that environment; the list shows every job of the environment to
  anyone who may read it (`READ_ENVIRONMENT`).
- **New fields of a run** (`runs list|show`, `GET /runs`): `job` (null for an ad-hoc `lakana
  run`), `trigger` (`schedule`, `api` or `manual`; null for an ad-hoc run), `scheduled_for` and
  `missed`. Runs written before the upgrade have null, null, null and 0.
- **New audit events:** `JOB_PAUSED`, `JOB_RESUMED`, `JOB_START_WAITING` (a start parked behind
  the active run) and `JOB_START_DROPPED` (a start that could not begin; its `reason` says why).
  `RUN_STARTED` gains the fields `job`, `trigger`, `scheduled_for`, `missed` and `merged` in its
  `detail`. Events written earlier do not have them.
- **New deploy output:** `Jobs:` in the diff of a deploy and a dry run (`jobs_added`,
  `jobs_removed`, `jobs_changed` in `diff` of `--output json`) and the deploy warnings of
  [the how-to](../how-to/run-a-job-on-a-schedule.md#the-warnings-of-a-deploy).
- **New command:** `lakana job list|run|pause|resume`.

### Notifications for jobs

A job can name a webhook in `lakana.yaml` (`notify:`) and get a message when a run fails and when
the job works again. Nothing is required of a project that declares none. See
[Get told when a job fails](../how-to/get-told-when-a-job-fails.md).

- **Migration `0029`** adds the table `notification_deliveries`, the queue and the record of the
  messages. Run the migrations as always.
- **A project with `notify:` needs workers of this release.** Upgrade every worker before the
  first deploy that declares `notify:`. A project version in which a job has `notify:` has manifest
  version 6, and a worker of the earlier release refuses its runs.
- **The worker depends on `httpx`** (a new dependency, permissively licensed), which sends the
  messages. A container image has it; a worker installed with `pip` gets it with the package.
- **New settings, on the worker:** `LAKANA_WEBHOOK__ALLOW_HOSTS`, `LAKANA_WEBHOOK__PROXY` and
  `LAKANA_WEBHOOK__CA_BUNDLE`, and `LAKANA_WORKER__NOTIFICATIONS` (on by default). A worker without
  a usable `LAKANA_SECRETS__MASTER_KEY` sends no notifications. See
  [Webhook network settings](../reference/network-settings.md#webhook-network-settings).
- **Without a setting, the worker sends only to a public `https` address on port 443.** If your
  chat server is inside your network, an operator has to allow its name.
- **New audit events:** `NOTIFICATION_FAILED` (a delivery gave up; its `detail` has the run, the
  job, the event and the reason) and `NOTIFICATION_TESTED` (someone ran `lakana job notify-test`).
- **New REST routes**, under `/api/v1/environments/{env}`: `POST /jobs/{job}/notify-test` (202;
  needs `DEPLOY`) and `GET /notifications/{id}` (needs `DEPLOY`, `RUN` or `READ_ENVIRONMENT` in
  the environment). Whoever may read a run sees its notifications in the run itself.
- **New field of a run** (`runs show`, `GET /runs/{id}`): `notifications`, a list with the delivery
  id, the event, the state, the attempts and a short result. It is empty for runs written before
  the upgrade.
- **New deploy warning:** a job that notifies through a secret that does not exist in the
  environment, or is revoked, still deploys; the warning names the job and the secret (see
  [the warnings of a deploy](../how-to/run-a-job-on-a-schedule.md#the-warnings-of-a-deploy)).
- **`notify` is part of a job's entry in the diff:** a deploy that adds, changes or removes a
  job's `notify:` lists the job under `jobs_changed` (`Jobs:` in the output of a deploy and a dry
  run).
- **An unreadable `LAKANA_WEBHOOK__CA_BUNDLE` stops the worker at start**, like the other CA
  bundles; fix the path before you start it.
- **`LAKANA_WEBHOOK__*` must be the same on every worker that delivers.** A worker without the
  allow-list refuses an internal address for good, so a delivery it claims first fails for that
  reason.
- **New commands:** `lakana job notify-test NAME` and `lakana job notification ID`.

### A cancel or a memory stop after a write sets the table back

A worker that stops itself for memory, or sees you cancel the run, after a task with checks wrote
its table no longer ends the task with the unchecked rows in the table. It hands the task back
(the wait reason `setting_back`), the next free worker sets the table back, and the task then
ends with the message of the stop or `The run was cancelled.` followed by what became of the
table. Before, only a worker that was lost got this. A run you cancel now ends only once a
pipeline worker has done it. See [A worker that is lost](../how-to/check-your-data.md#a-worker-that-is-lost).

### A task that ends because its run was cancelled says so in the run's words

A task that ends because you cancelled its run now reads `The run was cancelled.`, where it read
`The query was cancelled.` (the text for a query) or nothing at all, whether its worker stopped it,
the scheduler cancelled it while it waited, or a worker set its table back. A run that the
platform stopped because another task failed its code integrity check reads `The run was stopped
because another task failed its code integrity check.` instead, because nobody cancelled it. The
sentence about the table (`The table was set back to before this task ran.`) follows either text.
A Python task that is stopped this way reads the same, where it read `stopped: cancelled`. A
script that matches the old text on a task's message needs the new one. A query that you cancel
outside a run keeps `The query was cancelled.`

### An Iceberg commit cannot add a partition spec or sort order under a taken id

A commit through the Iceberg REST catalog that adds a partition spec or a sort order under an id
the table already has is now refused (422) for every caller, as a schema under a taken id already
was. Before, one whose content was identical went through. The refusal text says to load the table
again and repeat the change; loading needs `READ_TABLE`. A client that evolves the spec the normal
way (PyIceberg, for example) picks a new id, or makes the existing spec the default without adding it,
and is not affected. The refusal of removing schemas and partition specs now says why: Lakana keeps
every schema, partition spec and sort order a table has had. See
[the messages of a commit](../reference/checks.md#sdk-and-rest).

### A table that is kept after a check that could not run

Migration `0030` adds one column to `tables`; the migrations run before the server starts, as for
every upgrade. A table that carries the mark of a failed `keep` check and is built again, while its
`keep` check cannot run because it reads a table of another project that is kept, now moves its
mark to the new run, and `lakana table get` says that it holds data no check could run on. Before,
the mark kept naming the earlier run and check. Readers stay refused until the table passes its
checks. See [the rules of the mark](../reference/checks.md#the-rules).

### A set-back puts a kept table's earlier mark back

Migration `0031` adds two columns to `tables`; the migrations run before the server starts, as for
every upgrade. A kept table now remembers the marks it carried before its current one. When the
data the mark names is set back (a later run's `keep` failure moved the mark, and then that run
was cancelled before its failure was recorded, or its checks ran again and a `block` check failed),
the table takes the earlier mark back in the same step, when the table is set back to the data that
mark was a verdict on, and `lakana table get` names the run whose data the table holds again; if that
data had passed its checks, the table has no mark and tasks may read it. Before, the mark kept
naming the run whose data was gone. A table that was already kept when you upgrade has no
remembered marks: when its data is set back it gets a mark that says its checks are not known (see
the next section), and readers stay refused until the table passes its checks; build it again to
clear it. A script that reads `TABLE_ROLLED_BACK` events sees the new `kept_restored` key on such a
set-back. See [the rules of the mark](../reference/checks.md#the-rules).

### A set-back after another run's check no longer lets readers in

Migration `0032` adds one column to `tables`; the migrations run before the server starts, as for
every upgrade. A kept table whose mark was cleared by the check of another run, and whose data was
then set back to the earlier data that had failed its checks, used to carry no mark, so readers were
let in. Now a clear keeps the mark it removed, and a set-back to data that the table's current
verdict does not describe takes back the state that data had; when there is none (the history ran
out, or the table was marked before the history existed) the table gets a mark that reads `Set back
to data whose checks are not known`. Tasks are refused it like any mark, and the next passing checks
clear it: build the table again in the project that builds it. A script that reads
`TABLE_ROLLED_BACK` events sees `kept_unknown: true` on such a set-back, and `kept_not_restored` only
on a first build that kept its mark. See [the rules of the mark](../reference/checks.md#the-rules).

### A row filter is checked when it is created

No migration. Creating a row filter now refuses a column written with a qualifier
(`read_parquet.region`, `t.region`: write `region`) and a column the table does not have, as
creating a column mask already refused a column the table lacks. Names are compared without
regard to letter case, and a column written as a struct field (`address.country`) is refused with a
message of its own. Creating a column mask now refuses a qualified column in its expression the
same way. To read the table's columns, creating a filter that reads a column now needs
`READ_TABLE` on the table as well as `MANAGE_POLICY` (the `ADMIN` role has both). Create the table
with its columns first (`lakana table create NAMESPACE.TABLE --column NAME:TYPE`), then its filters and masks, then
write data or grant `READ_TABLE`: a table created with columns has a schema before any data, so
the filter can be created at once, and no row is readable unfiltered in between. A filter that
reads no column (`has_group('staff')`) is not affected. Filters and masks stored before the upgrade
are not looked at again: they keep applying as they did, and one that names a column the table no longer has still makes
every read of the table by its person fail instead of showing unfiltered rows. See
[Show some people only some rows](../how-to/filter-rows.md).

### Clearer messages when a read under a policy fails

No migration. Three raw database errors now read as Lakana's own sentences, and nothing is
newly refused or newly allowed:

1. **A table that gained a column leaves older data files without it.** Every read under a row
   filter or column mask on that table selects every column, so every such reader fails, whichever
   column the policy names. The message now says so ("The data files of `TABLE` do not all have
   the column `COLUMN` ...") and no longer lists the columns of one of the files, as the database
   does. It failed before too, never showing rows without the filter. A reader whose own SQL
   names the new column (`SELECT tier FROM ...`, or a subquery or view that leaves it out) gets
   the database's own line, without its list of candidates, and a hint that names the table and
   says its older files lack the column, the same whatever order the files are in.
2. **`lakana check rows`**: a check whose `SELECT` names a withheld column in another letter case
   or through a table alias (`o.note`) gets the sentence about the withheld column, as it did for
   the exact spelling; and a read that fails because maintenance removed the snapshot while the
   query was already reading it says the snapshot is gone. The query is asked again after a
   failure, and only an answer that the snapshot is gone changes the message.
3. A script that matched the database's text (`Referenced column ... not found`,
   `schema mismatch in glob`) on such a failure sees the new text instead.

See [Check your data](../how-to/check-your-data.md#look-at-the-rows-that-failed).

### The image runs Python 3.14

The Lakana image (`ghcr.io/lakana-labs/lakana`, used by the Compose bundle) moved from Python 3.13
to Python 3.14. A worker deploys a project with Python assets only when the project's
`requires-python` admits the worker's Python, so a project locked for 3.13 alone
(`requires-python = "==3.13.*"`, or `<3.14`) is refused with `uv.lock: requires-python '…' does
not admit the worker's Python 3.14.…`. Widen `requires-python` (the template of
`lakana project init` writes `>=3.11`), run `uv lock`, commit, and deploy again. Local mode
(`lakana serve`) runs on your own Python and is not affected.

### Delegation needs the ADMIN role

From this release only a principal with the `ADMIN` role on the instance can hand out access,
change groups and issue or revoke tokens. The three permissions behind this (`MANAGE_GRANT`,
`MANAGE_GROUP`, `MANAGE_TOKEN`) no longer work as single-action grants.

1. **Before you upgrade, make sure an administrator can sign in.** Run these commands as an
   administrator (the bundle's first administrator is `admin`, with the credential in the `team`
   profile). `lakana whoami` prints who your credential is. The JSON of `grant list` has one
   entry per grant: an administrator is a grant with `"role": "ADMIN"` and `"kind": "INSTANCE"`,
   for the principal in `principal_id` or, for a group, in `group_id`. The table of `grant list`
   names each subject (`user alice`, `group analysts`) where the caller may list principals and
   groups, and shows `principal <id>` or `group <id>` otherwise. To read the ids, list the
   principals and groups, then ask for the grants of each:

<!-- source: docs/examples/getting-started/run.sh#delegation-check -->
```bash
lakana whoami
lakana principal list
lakana --output json grant list
lakana group list
```

<!-- not tested: needs the name of a principal and a group of your instance -->
```bash
lakana grant list --principal NAME
lakana grant list --group GROUP
lakana group members GROUP
lakana token list NAME
```

   The members of a group with the `ADMIN` role are administrators too. The last command shows a
   principal's tokens: the key, the name and when it was last used. It does not show
   whether a token has expired or was revoked, and lists revoked ones too. The sure test is to use
   the token: the administrator signs in with it (`lakana login`) and runs `lakana whoami` and
   `lakana grant list`. If both print their table, the token works; if the server rejects the
   credential, it does not. Do this for at least one administrator before you upgrade.
2. **What stops working.** A principal or group that holds `MANAGE_GRANT`, `MANAGE_GROUP` or
   `MANAGE_TOKEN` as a single action can no longer grant access, manage groups or issue tokens,
   and a new grant of one of them on its own is refused. A grant of one of them that exists
   already is kept and does nothing. The server names up to twenty of them in a warning in its
   log when it starts (the line begins `N grant(s) of MANAGE_GRANT, MANAGE_GROUP or
   MANAGE_TOKEN as a single action have no effect`), and `lakana grant list` shows `no effect`
   in a `note` column for each, with a line under the table that says what it means and, for the
   first five, a note (the action and who holds the grant) followed by the command that revokes
   it with its whole id, alone on its own line so that you can copy it as it is (`--output json`
   has every grant and the full note, `no effect: <ACTION> needs the ADMIN role`). With
   `LAKANA_LOG__LEVEL=ERROR` the warning is not printed; the listing marks the grants in every case.
3. **What to do with them.** Give the `ADMIN` role to whoever should keep the ability, or let an
   administrator do those tasks, and revoke the dead grants. `ID` is the grant's id, from the
   lines under the table of `grant list` or from its JSON; for a group, use `--group` instead of
   `--principal`:

<!-- not tested: needs the name of a principal and the id of a grant of your instance -->
```bash
lakana grant create --kind INSTANCE --principal NAME --role ADMIN
lakana grant revoke ID
```

4. **The rule is not retroactive.** What a holder of one of the three created before the upgrade
   stays: grants, group memberships and tokens. To review them, read the audit log, one command
   per event type (the option takes one value). In the JSON, `principal_id` is the principal that
   acted and `detail` names the target (who was given the grant, which group and member, which
   token's owner):

<!-- source: docs/examples/getting-started/run.sh#delegation-audit -->
```bash
lakana --output json audit list --event-type GRANT_CREATED --all
lakana --output json audit list --event-type GROUP_MEMBER_ADDED --all
lakana --output json audit list --event-type PAT_ISSUED --all
```

   To look at one holder only, add `--principal-id ID` with the `principal_id` that the JSON of
   `grant list` shows for that holder's grant.
5. **If no administrator can sign in any more**, there is a way back that needs access to the
   database. It is what the current code requires and nothing more. At every start the server
   restores its first administrator (the principal named by `LAKANA_BOOTSTRAP_ADMIN`, `admin`
   unless you set it for the server; the Compose bundle does not) when no active principal holds
   the `ADMIN` role at `INSTANCE` scope, and no API call can leave an instance without one. So
   the way back revokes that role from **every** principal and then restarts the server.

   **The consequence:** after it, only the restored administrator holds the `ADMIN` role, and it
   has to give the role to the others again. Take a backup first (see
   [Backups](../how-to/deploy-with-compose.md#backups)). With the Compose bundle, in the folder of
   the bundle, first write down who holds the role now:

<!-- not tested: reads the database of a real instance -->
```bash
docker compose exec postgres psql -U lakana lakana -c \
  "SELECT id, principal_id, group_id FROM grants WHERE role = 'ADMIN' AND kind = 'INSTANCE' AND revoked_at IS NULL"
```

   Then revoke them and restart the server. **Run the statement once.** Run again after the
   restart it would revoke the restored administrator's role too:

<!-- not tested: changes a real database, and needs an instance whose administrators are locked out -->
```bash
docker compose exec postgres psql -U lakana lakana -c \
  "UPDATE grants SET revoked_at = now() WHERE role = 'ADMIN' AND kind = 'INSTANCE' AND revoked_at IS NULL"
docker compose restart server
docker compose logs server
```

   The server creates the restored principal, or reactivates it, gives it the `ADMIN` role,
   revokes its earlier tokens and issues one new token. **It never prints the token.** What
   happens depends on where the token comes from (the name is the one you configured):

   - The Compose bundle, and any server started with `LAKANA_BOOTSTRAP_PAT_KEY_ID`: the key id in
     `.env` was used by the token of the first start, and a revoked token keeps its key id, so the
     server **stops with an error** that names `LAKANA_BOOTSTRAP_PAT_KEY_ID` and no administrator
     is restored. Give it a pair never used before: in `.env`, replace
     `LAKANA_BOOTSTRAP_PAT_KEY_ID` with 16 new hex digits and `LAKANA_BOOTSTRAP_PAT_SECRET` with a
     new random string (for example the output of `openssl rand -hex 8` and of `openssl rand -hex 32`),
     then `docker compose up -d server`. The token that works is
     `lakana_pat_<LAKANA_BOOTSTRAP_PAT_KEY_ID>_<LAKANA_BOOTSTRAP_PAT_SECRET>`, made of the new
     values; the server logs only a DEBUG line (set `LAKANA_LOG__LEVEL` to `DEBUG` to see it).
     Sign in with it.
   - A server started with `LAKANA_BOOTSTRAP_TOKEN_FILE` and no key id: its log has a WARNING line
     that names the administrator, the key id and the file, and the token is in that file
     (mode 0600; read it as the server's user). **Delete the file** once you have signed in; the
     server refuses to start while a file is in the way.

   Sign in with the token, as in
   [Revoking the bootstrap credential](../how-to/deploy-with-compose.md#revoking-the-bootstrap-credential),
   and give the `ADMIN` role back to the others.

   A second restart does **not** issue another token, because an administrator exists again. If
   you lost the token, run the `UPDATE` once more and restart the server: it restores the
   principal again, with another new token (a new key id pair, or a new file, as above).
   Deactivating the principals instead of revoking their grants is not described here.

### Package sources are checked

Before this release no package source in a project's `uv.lock` was checked. Now a lock may name
only these sources: the public index (PyPI), the folder `wheelhouse` of the project, the
operator's extra index (`LAKANA_RUN__INDEX_URL`) and the package indexes an administrator
registered. See [Use a private package index](../how-to/private-package-index.md).

1. **A deployed project whose lock names any other index stops at its next execution**, and a new
   deploy of it is refused. A dependency from git, from a single file URL or from another local
   folder is refused the same way. The task fails with a message that begins `uv.lock: takes …
   from the package index …, which is not registered on this instance` and, for a project deployed
   before the upgrade, contains the sentence `The package indexes registered on this instance no
   longer match this project version`.
2. **What to do.** Two steps, both described on the page
   [Use a private package index](../how-to/private-package-index.md). First an administrator
   registers the index with `lakana index add` (the credential is optional). Registering alone
   is enough only for a project whose `pyproject.toml` already declares the index as a
   `[[tool.uv.index]]` table with `explicit = true`: its next run goes on. A project that
   declares the index as a plain `[[tool.uv.index]]` table, with `default = true`, or through
   `index-url` or `extra-index-url` then fails with a message that begins `pyproject.toml: the
   index …` and goes on `is a registered package index and needs`, or, for the two settings,
   one that names the setting and goes on `names the registered package index`. Such a project
   declares the index as a
   `[[tool.uv.index]]` table with `explicit = true`, names the packages that come from it under
   `[tool.uv.sources]`, and is locked (`uv lock`) and deployed again. An index that serves
   **every** package, such as a mirror of PyPI, does not belong in the registry: the operator
   sets it as `LAKANA_RUN__INDEX_URL` for the worker, and a lock may then name it without
   `explicit`. A git or URL dependency has to move to an index: publish it there, register the
   index and run `uv lock` again.
3. **The worker builds with `uv sync --frozen --no-config`.** It reads the lock and the index
   definitions in `[[tool.uv.index]]`, and ignores a project's `[tool.uv]` settings and any
   `uv.toml`, a system-level one on the worker's host included. For example these stop applying
   on the worker: `no-build`, `no-binary-package`, `required-version`, `compile-bytecode`, and
   proxy and TLS settings (the operator's `LAKANA_RUN__HTTPS_PROXY` and `LAKANA_RUN__CA_BUNDLE`
   still apply). They still shape the lock when you run `uv lock` on your machine. A `uv.toml` at
   the root of a project is refused at deploy, as before.
4. **A project that uses only PyPI and `wheelhouse` needs nothing,** unless it relied on one of
   the settings of item 3 on the worker: then that setting no longer applies there.

### Queries run in a process of their own

The worker no longer runs a query inside its own process. It starts a process for each query, and
it can end that process. That gives the time limit, a cancel and the memory limit something to act
on. How to use it: [Stop a query](../how-to/stop-a-query.md).

1. **Run the migrations; `0021` adds one column to `query_runs`.** The column keeps a default, so
   an old server or worker that still writes a query run while you upgrade goes on working: you do
   not have to stop them first. Going back: `0021`'s downgrade turns every `CANCELLED` run into a
   `FAILED` one with the cause `cancelled`.
2. **A query run has a fifth status, `CANCELLED`, which is final.** A program that waits for a
   query must treat it as an end. The SDK and the CLI of this release do; an older `lakana`
   package keeps polling a cancelled query until its own timeout, so upgrade the clients with the
   server.
3. **Each query costs about a tenth of a second more than before** (80 ms measured for
   `SELECT 42`): the worker starts a process for it.
4. **The time limit is hard.** A query that used to run on after
   `LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS` now ends, at the latest
   `LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS` (10, a new setting) later, with the cause `timeout`.
5. **A worker over its memory limit ends its largest consumer first** on Linux, a running query
   or a running Python task, whichever uses more memory. A query fails with the cause
   `out_of_memory` (with a new message); a task's attempt fails as transient with a new message
   and is retried within its attempts. The worker stops itself only if that was not enough or
   nothing could be ended (a task still building its environment cannot). Nothing to set. On
   macOS the worker stops as before.
6. **A worker that shuts down gives a running ad-hoc query**
   `LAKANA_RUN__DRAIN_GRACE_SECONDS` (20) and then ends it; before, it waited for the query however
   long it took. The Compose bundle's `stop_grace_period` of 45 s covers both graces.
7. **At start-up the worker starts one query process as a check.** A worker whose environment
   cannot start it stops with a message that names the cause.
8. **Limits that stay.** DuckDB's own memory setting is still a target, not a bound. On macOS and Windows the process
   handling has the limits it has for Python tasks. On macOS Lakana needs Python 3.13 or
   newer: with an older one `lakana server start --local` stops before it starts anything, and a
   worker started by hand stops at start, each with a message that says so. A query may read about 120,000 data files: the
   request the query process gets is limited to 64 MiB, and a query over it is refused before it
   starts, with a message that says to write the table again with `replace` so that its data lands in
   fewer, larger files.
9. **Ctrl-C in `lakana sql` and `lakana query wait` now cancels the query.** Before, the query ran
   on. For `lakana sql --into`, a Ctrl-C now stops the write: the table keeps the snapshot it
   had.
10. **Two audit entries are new.** A cancel request that the server accepts is audited as
    `QUERY_CANCELLED`. A cancel that is refused (the query is somebody else's, or does not exist)
    is recorded as `AUTHORIZATION_DENIED` with the reason `not_submitter`.

### Queries and deploys cannot get stuck

Before this release a query or a deploy whose worker was killed stayed `RUNNING` for ever, and a
cancel of it did nothing. Now every worker registers and writes a heartbeat to the database, and
the platform ends what a lost worker held. How it looks to a user:
[A worker that is lost](../how-to/stop-a-query.md#a-worker-that-is-lost). Why a query waits:
[Why is my query or task waiting?](../how-to/why-is-it-waiting.md).

1. **Run the migrations; `0022` adds the table `workers`, a column `worker_id` on `query_runs` and
   on `deploy_runs`, and two indexes on `query_runs`.** Nothing is converted. Going back: the
   downgrade drops them, and a run that was ended with the cause `worker_lost` keeps it, which an
   earlier release shows as a cause it does not know.
2. **Stop every worker of the old release before you migrate, and start the new ones after it.** A
   worker of the earlier release does not register and records no worker id on its runs, so the
   new scheduler ends its queries and its deploys once they started `LAKANA_RUN__LEASE_SECONDS`
   (60 s) ago. Mixed versions are not supported; this is the rule "Upgrade the server and every
   worker together" of [What every upgrade asks](#what-every-upgrade-asks). The steps for a
   Compose instance are in [Upgrading](../how-to/deploy-with-compose.md#upgrading).
3. **A query or a deploy that is `RUNNING` when you upgrade has no worker recorded.** The scheduler
   ends it once it started a lease ago. Every run that an earlier release left `RUNNING` for ever
   is therefore ended within about a minute after the new workers are up: a query shows `FAILED`
   with the cause `worker_lost`, a deploy shows `failed` and activated nothing. Nothing is run
   again by itself: run the query or deploy again.
4. **From now on a lost worker's work ends after about a minute.** A worker counts as lost when its
   last heartbeat is `LAKANA_RUN__LEASE_SECONDS` (60 s) old; it writes one every
   `LAKANA_RUN__RENEW_SECONDS` (20 s). The scheduler looks every
   `LAKANA_RUN__SCHEDULER_INTERVAL_SECONDS` (1 s) and ends what the lost worker held, with the
   cause `worker_lost` and a message that says to run it again. A scheduler that just took over, or
   was away for more than half a lease, first waits `LAKANA_RUN__RENEW_SECONDS` plus 5 s so that
   the workers can beat. A cancel of a query whose worker is gone is answered at once. A write
   that had already been committed ends `SUCCEEDED`, so that running it again does not write it
   twice; it shows fewer details than a write the worker finished itself (no watermark, no schema
   difference, no lineage summary). The write of a pipeline's SQL task is treated the same way,
   and the task with it: when the worker is lost after the write was committed, the task ends
   `succeeded` and is not run again, whichever attempt it is on and also while the run is being
   cancelled (a task whose table has [checks](../how-to/check-your-data.md) is the exception, see
   [Data quality checks](#data-quality-checks)). Its message says that the worker was lost afterwards; it has no watermark or
   schema change either, and its duration runs until the platform found the commit.
5. **Three settings have to fit together.** `LAKANA_RUN__LEASE_SECONDS` must have the same value
   for the server and for every worker. `LAKANA_RUN__RENEW_SECONDS` must be below it; a worker
   writes a warning to its log at start when it is more than half of it, because one late heartbeat
   would then make the worker count as lost. `LAKANA_RUN__SCHEDULER_INTERVAL_SECONDS` is the
   interval above. There is no new setting.
6. **The REST API and the SDK have four additions.** A queued query's status, and a task of a run
   that waits, carry `waiting_for` (a `code` and a `message`; `null` when nothing is waited for),
   and a failed run can have the cause `worker_lost`. Two list endpoints are new,
   `GET /api/v1/environments/{env}/queries` and `GET /api/v1/workers`, with the SDK methods
   `client.queries.list` and `client.workers.list`. An older `lakana` package ignores
   `waiting_for` and does not know the cause: it shows the message like for any cause it does not
   know.
7. **New commands.** `lakana query list` lists the queued and running queries of an environment
   (`--all` adds those that ended in the last 24 hours), and `lakana worker list` the workers and
   whether each is alive (it needs `MANAGE_ENVIRONMENT` on the instance). `lakana sql` and
   `lakana query wait` print `Waiting: <reason>` on standard error when a query has been queued
   for 3 seconds; they print nothing about it with `--output json`. See
   [Stop a query](../how-to/stop-a-query.md#find-a-query) and
   [Why is my query or task waiting?](../how-to/why-is-it-waiting.md).
8. **Limits that stay.** On a local instance, after the
   machine slept, a running query is ended if its worker sends no heartbeat within about half a
   minute after waking: run it again. For up to a minute after a worker died, `lakana worker list`
   and the reasons for waiting still count it as alive. The minute holds when another worker can
   take over the scheduler's role. Every database connection asks PostgreSQL to probe its client
   and to drop the connection after about two thirds of `LAKANA_RUN__LEASE_SECONDS` once its machine
   no longer answers, which is expected to free the role of a worker that lost power or its
   network. Before this release nothing
   asked, and PostgreSQL could keep such a connection for hours. A PostgreSQL that refuses these
   settings is logged once at the start of the worker or server and Lakana keeps running; then the
   old limit applies. `lakana scheduler status` shows which worker holds the role and when it last reported; if that worker is gone and the role stays with it for much longer than a minute, ending its database session in PostgreSQL (`pg_terminate_backend`), or restarting PostgreSQL, frees it; restarting the other workers does not.

### Workers for one kind of work, and a limit per user

A worker can now take only ad-hoc queries, or only pipeline work, and one user can run only so
many ad-hoc queries at once. How to use it:
[Separate workers for ad-hoc queries and pipelines](../how-to/deploy-with-compose.md#separate-workers-for-ad-hoc-queries-and-pipelines)
and [Why is my query or task waiting?](../how-to/why-is-it-waiting.md#a-queued-query).

1. **Run the migrations; `0023` adds the column `takes` to the table of workers.** Nothing is
   converted: an existing worker reads `all`, and takes every kind of work, as before.
2. **With one or two workers nothing changes.** That includes the Compose bundle's default of
   two. With **three or more workers that take ad-hoc queries**, one user or service account runs
   at most all but one of them at once by default: with three workers that take ad-hoc queries, one
   user can have two queries running at once; with four, three. A further query of
   that user waits and says `user_limit` while a worker is free; it starts when one of theirs
   ends. A script or a BI tool that runs queries in parallel under one account is such a user.
   To get the earlier behaviour back, set `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` to the
   number of workers, or to any number of 1 or more of your own. Set the same value on the server
   and on every worker.
3. **Two new settings.** `LAKANA_WORKER__TAKES` is `all` (the default), `interactive` or
   `pipeline`. `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` is `auto` (the default) or a whole
   number of 1 or more.
4. **The Compose bundle has two optional services**, `worker-interactive` and `worker-pipeline`,
   started by `LAKANA_WORKERS_INTERACTIVE` and `LAKANA_WORKERS_PIPELINE` in `.env` (both `0` by
   default). A bundle generated by an earlier release does not have them: generate a new one with
   `lakana bundle init` into an empty folder and copy the two services, or edit yours. The steps
   are in [Upgrading](../how-to/deploy-with-compose.md#upgrading). The bundle now needs Docker
   Compose 2.24 or later (it was 2.23): a service with no replica makes `docker compose up --wait`
   fail on 2.23.
5. **Upgrade the server and every worker together,** as
   [What every upgrade asks](#what-every-upgrade-asks) says. A worker of the earlier release
   does not register (see the note above): `lakana worker list` does not show it and the
   automatic limit does not count it. Stop every old worker before you migrate.
6. **The REST API, the SDK and the CLI gain** the field `takes` on a worker (`GET
   /api/v1/workers`, `client.workers.list`, `lakana worker list`) and two wait reasons,
   `no_worker_of_kind` (with `other_workers`) and `user_limit` (with `running` and `limit`). An
   older `lakana` package does not know the two codes and shows their sentence.
7. **Limits that stay.**
   - A queued deploy shows no reason why it waits, also when no worker takes pipeline work
     ([issue 92](https://github.com/lakana-labs/lakana/issues/92)); a later release added it.
     Look at the `takes` column of `lakana worker list`.
   - The limit per user is exact on PostgreSQL's default isolation level, which Lakana does not
     change.
   - Each worker applies its own value of the limit. Give the server and every worker the same
     one.
   - A worker still runs one query at a time. Several queries per worker are not part of this
     release ([issue 81](https://github.com/lakana-labs/lakana/issues/81)).

### Data quality checks

A project can now keep checks under `checks/`: [Check your data](../how-to/check-your-data.md).
What it asks of you:

1. **Run the migrations; `0024`, `0025` and `0026` are the three.** `lakana-server migrate` applies them as
   always, the Compose bundle's `migrate` service first, and a local instance when it starts.
   Every new column is empty or has a default, so an existing run, task or table reads as "no
   checks", and a table reads as one that keeps no data after a failed check (`0025`, see
   [Keep the data of a failed check](../how-to/check-your-data.md#block-keep-or-warn)). `0026` adds
   an empty column to the query runs; a query that was run before reads as showing no check's rows.
2. **A project without a `checks/` directory runs unchanged.** `lakana init` now writes one example
   check next to its example asset. If you delete the example asset, delete the check too, or the
   deploy is refused (see [Checks](../reference/checks.md#what-a-deploy-refuses)).
3. **A project with checks needs workers of this release.** Its project version has the manifest
   version 4. A worker of the earlier release refuses it with its message that says to update the
   worker, so upgrade the workers with the server, as every upgrade asks. A worker of the earlier
   release does not know `lakana check rows` either and would answer such a query from the table
   as it is now, so upgrade every worker before anyone uses the command.
   **The server too, before the first deploy of a project that has a `checks/` directory.** A run
   that a server of the earlier release starts for such a project cannot be checked: that server
   does not know checks. A worker of this release does not build such a table. Its task fails
   before anything is written, with "This run was started by a server that does not know checks,
   but the project has checks for `silver.orders`. Upgrade the server to the version of the
   workers, then run again." Upgrade the server, then run again.
4. **A task whose table has checks does not count as done when its worker is lost after the
   write.** Without checks, a write that was committed still ends the task `succeeded`
   ([Stop a query](../how-to/stop-a-query.md#a-worker-that-is-lost)). With checks, the task is
   taken again and runs only its checks, so a table that was written and not checked does not let
   the tables below it start.
5. **A commit that would leave a table with no current snapshot is refused,** also through the
   Iceberg REST catalog, because readers would see an empty table. So is a commit that publishes
   a table that a failed first build left not published by pointing at an existing snapshot. An
   engine that removed a table's `main` reference on purpose would now be refused; none is known.
   The message says to drop the table, or to write the rows it should hold.
6. **`lakana check rows` shows the rows a failed check found, for seven days.** A set-back keeps
   the failed state of the table under a tag named `lakana-check-failed-` and the run's
   identifier, and `lakana table maintain` removes the tag and its snapshot once the snapshot is
   more than seven days old, whatever `--keep-days` says. Look at the rows you need before then
   (see [Look at the rows that failed](../how-to/check-your-data.md#look-at-the-rows-that-failed));
   the number of rows stays in `lakana check list`. The report of `lakana table maintain` lists the
   removed tags as `tags_removed`.
7. **Nothing is converted.** A table is checked only by the checks you write for it.

### The log escapes control characters

Both processes now write control characters in a log record as text (an escape as `\x1b`, a
carriage return as `\x0d`; a tab becomes four spaces), so that a name, a path or a library's
message cannot forge a line or recolour a terminal. What changes for you:

1. **Every line after the first of a multi-line record, such as a traceback, starts with four
   spaces and `| `.** A search or an alert rule that matched a traceback line by its start (for
   example `^Traceback`) needs to allow the prefix. A line that does not start with a space is
   the start of a record.
2. **Uvicorn's level names have no colour on a terminal any more.**
3. **A record longer than 1 MiB of text is cut**, with a count of what was left out.

The details are in the paragraph "Control characters in the log" of
[Deploy with Docker Compose](../how-to/deploy-with-compose.md).
