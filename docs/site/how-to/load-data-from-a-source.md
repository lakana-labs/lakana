# Load data from a source

This page brings the data of another system into the bronze layer: files a system exports, the
tables of a database or the records of a REST API. Each source is read by a short Python asset
that you keep in your project. Lakana runs no connectors. The asset calls a helper of
`lakana.ingest`, and each run reads only what is new since the last one. A job in `lakana.yaml`
runs it on a schedule.

The steps:

1. Create a connection: where the source is, and its password or token.
2. Write the asset: `lakana init --template` writes one for files, a SQL database or a REST API.
3. Lock the project's packages with `uv lock`.
4. Deploy and run.
5. Put it on a schedule.

For one file you have on your machine, you do not need any of this: `lakana load` loads it into
bronze with one command ([Loading a local file](../reference/load.md)).

## Before you start

A Python asset needs `uv` on your machine, and the public package index for `uv lock` and for
the worker's first build ([Python assets](../reference/python-assets.md#dependencies)).

A `directory` or `sqlite-file` connection reads below an **ingestion root**, a directory that the
operator gives the worker by name. The other kinds need none. For a local instance, name the root
when you start it:

<!-- source: docs/examples/ingest-files/start.sh#start -->
```bash
LAKANA_RUN__INGESTION_ROOTS="{\"landing\": \"$HOME/landing\"}" lakana server start --local
```

With the Docker runner, the operator sets `LAKANA_LAUNCHER__INGESTION_ROOTS` instead
([Ingestion roots](run-python-tasks-in-containers.md#ingestion-roots)); in the Compose bundle that
is `LAKANA_INGESTION_ROOTS` in `.env` ([Files on this host](deploy-with-compose.md#files-on-this-host)).

## 1. Create the connection

The example on this page reads the CSV files that a shop system exports into
`~/landing/exports`. A `directory` connection names the root and the directory below it:

<!-- source: docs/examples/ingest-files/run.sh#connection -->
```bash
lakana connection create exports --env dev --kind directory --root landing --path exports
```

<!-- output: ingest-files#connection -->
```text
Created connection exports in dev
```

A database or an API needs its password or token too. `--password-stdin` reads it from standard
input, so it never appears in your shell's history. The examples further down show both, and
[Connections](../reference/connections.md) lists every kind and its options.

Test the connection before you write code that reads it. A worker checks that it can reach the
source and, for a source with a secret, that the source accepts the secret:

<!-- source: docs/examples/ingest-files/run.sh#connection-test -->
```bash
lakana connection test exports --env dev
```

<!-- output: ingest-files#connection-test -->
```text
ok: the path `exports` is readable under root `landing` (2 entries)
```

Any other outcome names what failed: the host name, the port, the certificate, the root or the
secret ([what each outcome means](../reference/connections.md#test-a-connection)).

## 2. Write the asset

`lakana init --template` creates a project whose one asset reads a source:

<!-- source: docs/examples/ingest-files/run.sh#init -->
```bash
cd ~
lakana init sources --template ingest-files
cd sources
```

| Template | Asset | Reads | Write mode |
|---|---|---|---|
| `ingest-files` | `assets/bronze/exports.py` | the CSV files of a `directory` connection, with `files.read` | `append`, the files changed since the last run |
| `ingest-sql` | `assets/bronze/orders.py` | a table of a PostgreSQL, MariaDB or SQLite database, with `sql.read` | `merge` on `id`, the ids from 1,000 before the largest one loaded |
| `ingest-rest` | `assets/bronze/items.py` | a JSON REST API, with `rest.read` | `merge` on `id`, the items updated since an hour before the newest one loaded |

Each project also gets `lakana.yaml`, the CI files, a `pyproject.toml` that depends on
`lakana[assets]` at the version that wrote it, a test that checks every Python asset parses, and a
`README.md` with the next steps for its connection. The command prints the same steps.
These templates leave out the sample project, and `--sample` with them is refused. (The fourth
template, `python`, is the sample with a Python asset:
[Build a table with Python](../tutorials/python-sample.md).)

The asset of `ingest-files`:

<!-- source: docs/examples/ingest-files/project/assets/bronze/exports.py -->
```python
"""Rows of the CSV files a source system exports into a directory."""

from lakana import asset
from lakana.ingest import files


@asset(connections=["exports"], write="append", watermark="_source_modified")
def exports(ctx):
    """Rows of the exported CSV files, appended as new files arrive."""
    # files.read adds `_source_modified`, the time each file was last changed, and with since=
    # reads only files changed after it. On the first run ctx.watermark() is None, which is
    # right here: every file is new, so every file is read.
    return files.read("exports", "*.csv", since=ctx.watermark())
```

- `connections=["exports"]` declares the connection. A task reads only the connections its asset
  declares, and a deploy is refused while the environment lacks one of them.
- `write="append"` adds each run's rows to the table; `watermark="_source_modified"` is the column
  whose largest value the next run starts after. `ctx.watermark()` returns that value.

Change the pattern, the connection's name and the function to fit your source. The table is named
after the file: `bronze.exports`.

## 3. Lock the packages

<!-- source: docs/examples/ingest-files/run.sh#lock -->
```bash
uv lock
```

It ends with `Resolved N packages`, where N depends on the packages on the index, and writes
`uv.lock`. The worker installs exactly the packages it names, so commit it with the asset.

## 4. Deploy and run

<!-- source: docs/examples/ingest-files/run.sh#deploy -->
```bash
git init --quiet
git add -A
git commit --quiet -m "Read the exported orders"
lakana repo add sources --kind local --url "$PWD"
lakana deploy --env dev
```

<!-- output: ingest-files#deploy -->
```text
Registered repository sources
Queued deploy 94a51535-846d-4977-b480-951f7c9a9f6e
Deployed sources in dev at 26d2de7c0e8249fb40631e8af7eb548849c2fb0a
┏━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ change ┃ table          ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ added  │ bronze.exports │
└────────┴────────────────┘
Hint: run 'lakana run --env dev' to run what you just deployed.
```

<!-- source: docs/examples/ingest-files/run.sh#run -->
```bash
lakana run --env dev
```

The first run builds the asset's Python environment, which takes a minute or more, and reads
both files: the run's summary shows 3 rows. When a third file arrives, run again:

<!-- source: docs/examples/ingest-files/run.sh#run-again -->
```bash
lakana run --env dev
```

This run reads only the new file and adds its 2 rows. The table now holds every order once:

<!-- source: docs/examples/ingest-files/run.sh#query -->
```bash
lakana sql "SELECT order_id, customer, amount, _source_modified FROM bronze.exports ORDER BY order_id"
```

<!-- output: ingest-files#query -->
```text
┏━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ order_id ┃ customer ┃ amount ┃ _source_modified          ┃
┡━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 1        │ Ada      │ 12.5   │ 2026-10-01T08:00:00+00:00 │
│ 2        │ Grace    │ 8.0    │ 2026-10-01T08:00:00+00:00 │
│ 3        │ Linus    │ 20.0   │ 2026-10-01T08:00:00+00:00 │
│ 4        │ Ada      │ 5.25   │ 2026-10-02T08:00:00+00:00 │
│ 5        │ Barbara  │ 9.75   │ 2026-10-02T08:00:00+00:00 │
└──────────┴──────────┴────────┴───────────────────────────┘
5 rows
```

A run that finds nothing new succeeds and writes nothing. A file is new when it changed strictly
after the newest file already loaded. A file copied in with its old time (`cp -p`, `rsync -t`) or
moved in with `mv` keeps that time and is not read:
[Reading only new files](../reference/ingest.md#reading-only-new-files) says when that happens
and how to read by the file's name instead.

## 5. Run it on a schedule

Add a job to `lakana.yaml`:

<!-- source: docs/examples/ingest-files/jobs.yaml -->
```yaml
jobs:
  load_exports:
    select: ["bronze.exports"]
    schedule:
      cron: "0 * * * *"
```

Commit and deploy:

<!-- source: docs/examples/ingest-files/run.sh#schedule -->
```bash
git commit --quiet -am "Load the exports every hour"
lakana deploy --env dev
```

<!-- output: ingest-files#schedule -->
```text
Queued deploy 8cc564a8-cdb7-4df6-9c17-62d8c00abe7d
Deployed sources in dev at 28119fe59494743163fa849406b67c3c026fcbb9
Jobs:
  added load_exports
warning: job `load_exports` has a schedule but no run principal is bound in `dev`; it does not run until an admin binds one with `lakana deployments set-run-as`
Hint: run 'lakana run --env dev' to run what you just deployed.
```

As the warning says, a schedule runs only once an administrator binds a run principal to the
project. [Run a job on a schedule](run-a-job-on-a-schedule.md#bind-a-run-principal) shows how,
and how to pause the job.

## Choose a helper

| Source | Connection kinds | Helper | Template |
|---|---|---|---|
| Files in a directory, a bucket or on an HTTPS server | `directory`, `s3`, `gcs`, `azure`, `http` | [`files.read`](../reference/ingest.md#filesread) | `ingest-files` |
| A table of a database | `postgresql`, `mariadb`, `sqlite-file` | [`sql.read`](../reference/ingest.md#sqlread) | `ingest-sql` |
| A JSON REST API | `http` | [`rest.read`](../reference/ingest.md#restread) | `ingest-rest` |
| Anything else | any, `tcp` included | [`connections.get`](../reference/connections.md#how-a-task-gets-its-credential) and your own client | none |

### A SQL database

The asset of `ingest-sql` reads the orders whose `id` is larger than the largest one loaded, less
`LOOK_BACK`, and merges them into the table on `id`:

<!-- source: docs/examples/ingest-sql/project/assets/bronze/orders.py -->
```python
"""Orders from a source database, merged on their id as new ones arrive."""

from lakana import asset
from lakana.ingest import sql

# How many ids behind the largest one loaded each run reads again. A row gets its id when it is
# written but is seen only once its transaction commits, so a row with a smaller id can appear
# after a run read larger ones; without reading again it would be skipped for good. The merge on
# `id` keeps one row per id. Make it larger than the ids the source hands out while one of its
# transactions is open.
LOOK_BACK = 1000


@asset(connections=["source-db"], write="merge", key=["id"], watermark="id")
def orders(ctx):
    """Orders from the source database: each run reads from LOOK_BACK ids before the last one."""
    # For rows that change after they were written, use a change column as the watermark:
    # watermark="updated_at" with "... WHERE updated_at > :since", a datetime default, and the
    # look-back as a timedelta.
    return sql.read(
        "source-db",
        "SELECT * FROM orders WHERE id > :since ORDER BY id",
        params={"since": ctx.watermark(default=0) - LOOK_BACK},
    )
```

Why not simply append the ids after the largest one? A database hands out an id when a row is
written, but other sessions see the row only when its transaction commits. A transaction that
took id 10 and commits after another one took and committed id 11 shows its row late: a run in
between reads 11, the next one starts after 11, and row 10 is never read, with no error. Reading
again from `LOOK_BACK` ids behind the watermark finds such a row, and the merge on `id` keeps one
row per id however often it is read. Make `LOOK_BACK` larger than the ids your source hands out
while one of its transactions is open; the cost is reading those rows again on every run.

For rows that change after they were written, use the variant in the comment: a change column as
the watermark, with a look-back as a `timedelta`. The value goes in the `:since` placeholder,
never into the text of the query.

The template's `README.md` creates a PostgreSQL connection, which asks for the password on
standard input. The tested recipe reads a SQLite file below the ingestion root instead, which
needs no server and no password:

<!-- source: docs/examples/ingest-sql/run.sh#connection -->
```bash
lakana connection create source-db --env dev --kind sqlite-file --root landing --path shop.db
```

<!-- output: ingest-sql#connection -->
```text
Created connection source-db in dev
```

In the recipe the first run reads orders 1, 2 and 4: order 3 had its id but had not committed.
Then order 3 commits and order 5 arrives. The second run reads both, and every order is in the
table once:

<!-- source: docs/examples/ingest-sql/run.sh#query -->
```bash
lakana sql "SELECT id, customer, amount FROM bronze.orders ORDER BY id"
```

<!-- output: ingest-sql#query -->
```text
┏━━━━┳━━━━━━━━━━┳━━━━━━━━┓
┃ id ┃ customer ┃ amount ┃
┡━━━━╇━━━━━━━━━━╇━━━━━━━━┩
│ 1  │ Ada      │ 12.5   │
│ 2  │ Grace    │ 8.0    │
│ 3  │ Linus    │ 20.0   │
│ 4  │ Ada      │ 5.25   │
│ 5  │ Barbara  │ 9.75   │
└────┴──────────┴────────┘
5 rows
```

The table's lineage names the connection it was read from:

<!-- source: docs/examples/ingest-sql/run.sh#lineage -->
```bash
lakana table lineage bronze.orders --env dev
```

<!-- output: ingest-sql#lineage -->
```text
bronze.orders
└── connection source-db (sqlite-file)
```

### A REST API

The asset of `ingest-rest` asks the API for the items updated since the last run and merges them
into the table on `id`:

<!-- source: docs/examples/ingest-rest/project/assets/bronze/items.py -->
```python
"""Items from a source REST API, merged on their id as they change."""

from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.compute as pc

from lakana import asset
from lakana.ingest import rest

# How far behind the newest `updated_at` loaded each run asks again. The source sets updated_at
# when it writes an item, but the API shows the item only once that write commits, so an item
# can appear after a run read newer ones; without asking again it would be skipped for good.
# The merge on `id` keeps one row per item. Make it longer than the source's longest write.
LOOK_BACK = timedelta(hours=1)


@asset(connections=["source-api"], write="merge", key=["id"], watermark="updated_at")
def items(ctx):
    """Items changed since LOOK_BACK before the last run's newest, merged on their id."""
    since = ctx.watermark(default=datetime(2000, 1, 1, tzinfo=UTC)) - LOOK_BACK
    changed = rest.read(
        "source-api",
        "items",
        params={"updated_since": since},
        paginate=rest.NextLink(),
        records="value",
    )
    if "updated_at" not in changed.column_names:
        return changed  # nothing changed since the last run: the run writes nothing
    # JSON has no timestamp type: the API's ISO 8601 text becomes one, so it can be the watermark.
    at = changed.schema.get_field_index("updated_at")
    return changed.set_column(
        at, "updated_at", pc.cast(changed["updated_at"], pa.timestamp("us", "UTC"))
    )
```

- `"items"` is the path below the connection's base URL; it is relative, without a leading `/`.
- `paginate=rest.NextLink()` follows the link each page holds, and `records="value"` is where a
  page keeps its list (OData's names). Change both, and the parameter's name, to what your API
  uses ([Pages](../reference/ingest.md#pages)).
- `LOOK_BACK` asks again for the hour before the newest `updated_at` loaded, for the same reason
  as the SQL template's: the source sets `updated_at` when it writes an item, but the API shows
  the item only once the write commits, so an item can appear after a run read newer ones, with
  an older time. The merge on `id` keeps one row per item however often it is read. Make it
  longer than the source's longest write.
- JSON keeps a time as text, and text cannot be a watermark, so the asset turns `updated_at` into
  a timestamp. When nothing changed, the API answers an empty list and `rest.read` returns a
  table without columns: the asset returns it as it is, and the run succeeds without writing.

An API with a bearer token: the connection takes its base URL, and the token on standard input.
The recipe's API runs on the same machine, with a certificate of its own CA, so the connection
gets that CA too (`--ca-file`):

<!-- source: docs/examples/ingest-rest/run.sh#connection -->
```bash
lakana connection create source-api --env dev --kind http --url "https://localhost:$API_PORT/api" \
  --auth bearer --ca-file ca.pem --password-stdin < token.txt
```

<!-- output: ingest-rest#connection -->
```text
Created connection source-api in dev with its secret source-api-connection
```

`localhost:$API_PORT`, `ca.pem` and `token.txt` belong to the recipe's stand-in API. For your API,
give its base URL, such as `--url https://api.example.com/v2`, and leave out `--ca-file` when a
public CA signed its certificate. Pipe the token in from where you keep it, or type it after the
command and end the input with Ctrl-D.

After a first run, one item changed and one was added. The second run merged both: the change
replaced its row, and the new item was added:

<!-- source: docs/examples/ingest-rest/run.sh#query -->
```bash
lakana sql "SELECT id, name, price, updated_at FROM bronze.items ORDER BY id"
```

<!-- output: ingest-rest#query -->
```text
┏━━━━┳━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ id ┃ name        ┃ price ┃ updated_at                ┃
┡━━━━╇━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 1  │ Anchor      │ 24.5  │ 2026-10-01T08:00:00+00:00 │
│ 2  │ Buoy, large │ 9.25  │ 2026-10-02T08:00:00+00:00 │
│ 3  │ Cleat       │ 3.75  │ 2026-10-01T10:00:00+00:00 │
│ 4  │ Deck mop    │ 12.5  │ 2026-10-02T09:00:00+00:00 │
└────┴─────────────┴───────┴───────────────────────────┘
4 rows
```

### Anything else: your own client

For a source no helper reads, `connections.get` gives the asset the connection's address,
settings and secret, and under the Docker runner the proxy or relay the task must use. This asset
reads the same API with `httpx`:

<!-- source: docs/examples/ingest-generic/project/assets/bronze/items.py -->
```python
"""Items from a source REST API, read with your own HTTPS client."""

import os
import ssl

import httpx
import pyarrow as pa

from lakana import asset
from lakana.ingest import connections


@asset(connections=["source-api"])
def items():
    """Every item, read with httpx from what the connection holds."""
    api = connections.get("source-api")
    # Trust what the helpers trust: the workers' bundle (the system's roots without one) and the
    # connection's own CA.
    trust = ssl.create_default_context(cafile=os.environ.get("LAKANA_CA_BUNDLE"))
    trust.minimum_version = ssl.TLSVersion.TLSv1_2
    if api.settings.get("ca_certificate"):
        trust.load_verify_locations(cadata=api.settings["ca_certificate"])
    base = httpx.URL(str(api.address["base_url"]).rstrip("/") + "/")
    rows, url = [], base.join("items")
    with httpx.Client(
        headers={"Authorization": f"Bearer {api.secret['token']}"},
        proxy=api.proxy_url,  # the task's egress proxy under the Docker runner; None otherwise
        trust_env=False,  # no proxy, certificate or .netrc from the task's environment
        verify=trust,
        timeout=30,
    ) as client:
        while url is not None:
            # The token goes with every request: never send it below another address.
            if not str(url).startswith(str(base)):
                raise ValueError(f"the next link {url} is not below {base}")
            page = client.get(url)
            page.raise_for_status()
            body = page.json()
            rows += body["value"]
            link = body.get("@odata.nextLink")
            url = None if link is None else url.join(link)
    return pa.Table.from_pylist(rows)
```

- For SQL Server, [Read from a SQL Server database](read-from-a-sql-server-database.md) is a tested
  recipe of this kind, with its driver, TLS and types.
- Pass `proxy_url` to your client: under the Docker runner it is the only way out of the task,
  and `trust_env=False` keeps a proxy or certificate from the environment out of it. A database
  client connects to `relay_address` instead of the host (see
  [What a task reaches](../reference/connections.md#what-a-task-reaches-and-how)).
- The secret's values are hidden as `***` in what the task prints. A value your code derives from
  them, such as a header built from a password, is not.
- Without `write=`, each run replaces the table with every item, and `updated_at` stays text:

<!-- source: docs/examples/ingest-generic/run.sh#query -->
```bash
lakana sql "SELECT id, name, price, updated_at FROM bronze.items ORDER BY id"
```

<!-- output: ingest-generic#query -->
```text
┏━━━━┳━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ id ┃ name   ┃ price ┃ updated_at           ┃
┡━━━━╇━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ 1  │ Anchor │ 24.5  │ 2026-10-01T08:00:00Z │
│ 2  │ Buoy   │ 7.25  │ 2026-10-01T09:00:00Z │
│ 3  │ Cleat  │ 3.75  │ 2026-10-01T10:00:00Z │
└────┴────────┴───────┴──────────────────────┘
3 rows
```

## Choose a write mode

| `write=` | What each run writes | Needs | A run that reads nothing new |
|---|---|---|---|
| `replace` (the default) | Every row of the table, replaced by what the asset returns. | nothing | Replaces the table with what it returned, so read everything each time. |
| `append` | The rows the asset returns, added to the table. | `watermark=` | Succeeds and writes nothing. |
| `merge` | Each returned row replaces the row with its `key=`, or is added. Nothing is deleted. | `key=`; `watermark=` is optional | Succeeds and writes nothing. |

Unlike a SQL asset, a Python asset filters its own rows: it must return only the rows past
`ctx.watermark()`, as the templates do with `since=` and a query parameter. A row returned twice
by an `append` asset is in the table twice, so an asset that reads behind its watermark, as the SQL
template does, merges on a key. The watermark is a column of the result, of an
integer, decimal, date or timestamp type. The rules in full:
[Write modes](../reference/asset-front-matter.md#write-modes).

## The first run: `ctx.watermark(default=...)`

On the first run there is no watermark yet, and `ctx.watermark()` returns `None`.
`ctx.watermark(default=...)` returns your value instead. `sql.read` and `rest.read` refuse `None`
as a parameter: compared with a column, it would match no row on the first run and every run
after it. The default is the start of the source's history: `0` for an id, an early date for a
time. `files.read` is the exception: `since=None` reads every file, which is right for a first run.

## Under the Docker runner

With the Docker runner (the Compose bundle's default), each task runs in a container that reaches
exactly the endpoints of the connections its asset declares, and nothing else:

- HTTPS goes through the `egress` service, which must be running; databases go through a relay of
  the task's own; directories are read-only mounts of the ingestion roots.
- An address inside the Lakana stack or on the Docker host (its gateway,
  `host.docker.internal`, `localhost`) is refused, whatever the asset declares. Give the source an
  address outside them.

The full rules: [Connections: what a task reaches](run-python-tasks-in-containers.md#connections-what-a-task-reaches).
Under the local runner a task connects directly, as the examples on this page do.

## When something goes wrong

| Message | Cause | Next step |
|---|---|---|
| ``Asset `bronze.exports` declares connection `exports`, which environment `prod` does not have. Create it: ...`` | The deploy's environment has no connection of that name. | Create it in that environment with the command the message names, then deploy again. |
| ``connection `crm-db` is used by `analytics: customers` in `prod`; deploy a version without it first.`` | A deployed asset still declares the connection you are deleting. | Remove it from `@asset(connections=[...])`, deploy, and delete again. |
| ``` `write: append` needs `watermark:` — the column whose largest value the next run starts after ``` | `write="append"` without `watermark=`. | Name the column, such as `watermark="id"`. |
| ``watermark column `updated_at` is a string; it must be one of int, long, decimal, date, timestamp, timestamptz`` | The watermark column is text, as JSON times are. | Cast it, as the REST template does with `pc.cast`. |
| `parameter since is None, so the comparison would match no row` (or ``params= since is None, which would send an empty value``) | The first run's `ctx.watermark()` went into a query or a request. | Pass `ctx.watermark(default=...)`. |
| ``` `crm-db` is not declared by this asset: add it to @asset(connections=[...]) and deploy. ``` | The code reads a connection its asset does not declare. | Add it to `connections=` and deploy. |
| ``connection `crm-db` is not declared by asset `bronze.customers` `` (or ``is not in environment `dev` ``, `only a running task can fetch a connection`) | The fetch of the credential was refused: the deployed asset does not declare it, the connection was deleted since the deploy, or the token is not a running task's. | Declare the connection and deploy, or create it again. Read connections yourself with `lakana connection show`. |
| ``` `crm-db`: authentication failed (PostgreSQL said: ...). Check the secret with `lakana connection test crm-db --env prod`. ``` | The source refused the password or token. | Test the connection, and give the secret again with `lakana connection update ... --password-stdin`. |
| ``` `crm-db`: the server's certificate is not trusted for `db.example.com`. Give the connection your CA: `lakana connection update crm-db --env prod --ca-file ca.pem`. ``` | A CA the task does not trust signed the source's certificate, or it is for another name. | Run the command the message names with your CA's file. |
| ``` `exports` names ingestion root `landing`, which this worker does not have. Set it in `LAKANA_LAUNCHER__INGESTION_ROOTS` (Docker runner) or `LAKANA_RUN__INGESTION_ROOTS` (local runner). ``` | The worker that took the task has no root of that name. | Ask the operator to add it to the setting the message names, and restart the worker or the launcher. |
| ``api.example.com:443 is not a destination this task may reach; declare it in @asset(connections=[...]) ...`` in the task's log, or ``` `api.example.com` is not a connection of this asset: ... ``` | Under the Docker runner, the task tried a host that none of its connections names, or one inside the Lakana stack or on the Docker host. | Declare the connection that names the host; give the source an address outside the stack. |
| ``tasks with connections need the `egress` service; start it (Compose bundle) or remove the connection from the asset.`` | Under the Docker runner, the `egress` service is not running. | Start it with the Compose bundle. |
| ``key column `id` is null in 3 rows`` or ``key `id` repeats in 12 rows of this batch; make it unique ...`` | A `merge` asset returned rows without a key, or several rows with one key. | Drop the rows without a key, and keep one row per key, such as the newest. |
| ``` `lakana load` loads into bronze only: ... ``` | `lakana load` was given a table outside bronze. | Load into bronze and transform it with an asset ([Loading a local file](../reference/load.md)). |

Each helper's own errors, and whether a run retries them, are in
[The ingest helpers](../reference/ingest.md).
