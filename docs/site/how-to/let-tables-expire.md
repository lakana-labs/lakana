# Let tables expire

After this guide you can give a table a retention, so that Lakana drops it once nobody has used it
for a while, set one retention for every table of an environment, see what expires soon, and keep
a table that should stay. It is meant for development and exploration: the test load, the scratch
table of a query, the tables of a project someone tried once.

Nothing expires unless someone sets a retention. An expired table is dropped like any other: its
files stay for the undo period (seven days by default), and during that time
`lakana table undrop` brings it back with its data and its access rules. See
[Drop and restore a table](drop-and-restore-a-table.md).

## Let a new table expire

Add `--expire-after` with a number of days when you create the table:

<!-- source: docs/examples/let-tables-expire/run.sh#create -->
```bash
lakana table create bronze.scratch --column id:long --expire-after 14
```

A write that creates its table takes the same option:

<!-- source: docs/examples/let-tables-expire/run.sh#sql -->
```bash
lakana sql "SELECT 1 AS id" --into bronze.trial --mode create --expire-after 7
```

The table expires that many days after its **last activity**: its last write, or its last read
through a query, whichever is later (see [What counts as activity](#what-counts-as-activity)).
`--expire-after` goes with `--mode create` only; for a table that exists, use
`lakana table retention`, below.

## See a table's retention

<!-- source: docs/examples/let-tables-expire/run.sh#show -->
```bash
lakana table retention bronze.scratch
```

The answer says when the table expires, the rule (and whether the table or its environment sets
it), who set it, and the table's last activity. A table that sets nothing and whose environment
has no retention does not expire.

## Change it

Give the table a number of days, a day, or no expiry at all:

<!-- source: docs/examples/let-tables-expire/run.sh#days -->
```bash
lakana table retention bronze.trial --days 3
```

<!-- source: docs/examples/let-tables-expire/run.sh#never -->
```bash
lakana table retention bronze.trial --never
```

- `--days N` (1 to 3,650): the table expires N days after its last activity.
- `--on YYYY-MM-DD`: the table expires from midnight UTC of that day on, whatever its activity.
  The day must be after today; to drop a table now, drop it.
- `--never`: the table never expires, whatever its environment's retention says.

A change of the retention counts as activity, so `--days 3` means three days from now at the
earliest.

**Who may.** The table's owner (who created it) may set any retention. Anyone else needs:

- the right to drop the table (the data engineer and admin roles have it) to make it expire
  **sooner**, or at all when it would not expire now;
- the right to write to it to make it expire **later**, or `--never`. So whoever works with a
  table can always keep it.

## Let an environment's tables expire

A retention on an environment applies to every managed table there that sets none of its own:

<!-- source: docs/examples/let-tables-expire/run.sh#env -->
```bash
lakana env retention dev --days 30 --yes
```

Without `--yes` the command asks first. A table never expires sooner than the days after the
retention was set, so turning it on gives every table its full period, however old it is. A table's
own retention (and `--never`) wins over the environment's. Setting it needs the right to manage
the environment. Turn it off with:

<!-- source: docs/examples/let-tables-expire/run.sh#env-off -->
```bash
lakana env retention dev --off
```

Without an option, `lakana env retention dev` shows the environment's retention.

## See what expires soon

<!-- source: docs/examples/let-tables-expire/run.sh#expiring -->
```bash
lakana table expiring --within 7d
```

It lists the tables you may read that expire within the window, soonest first, with when, the
rule and who set it. Without `--within` the window is the server's warning window,
`LAKANA_TABLES__EXPIRY_WARNING_DAYS` (7 days by default). Within that window, `lakana table list NAMESPACE`
shows an `expires` column (the list across namespaces has none) and `lakana table get` says when the table expires and how to keep it.

## What counts as activity

A table's last activity is the latest of:

- its last change in the catalog: a write (a load, an append, a replace, a pipeline run, a commit
  from an engine through the Iceberg REST catalog), its maintenance, and a change of its
  description or its retention;
- its last read by a query that succeeded, through `lakana sql`, the SDK, a Python task, or a SQL
  task. These are the reads that [table health](../reference/table-health.md) counts.

A read only ever moves the expiry later. Two kinds of read are not seen, so they do not keep a
table:

- **reads through the Iceberg REST catalog** (an engine such as Spark or Trino reading the table
  directly), which Lakana does not record;
- **reads older than `LAKANA_TABLE_READS__KEEP_DAYS`** (90 days by default), which the worker
  deletes. With a retention longer than that, a table read only long ago counts from its last
  write.

## What never expires

- **Tables in a protected environment**, even one whose retention was set before the protection.
  Setting a retention there is refused; `--never` is still accepted.
- **Tables a deployed project builds** (an asset of the project version that is current in the
  environment): the next run would build them again.
- **A table that a write is running on or queued for**, or whose pipeline task has not ended: the
  worker waits for the next clean-up.
- **Views, external tables and artifacts**: only managed tables have a retention.

## When a table is dropped

The worker that holds the scheduler role drops expired tables in its clean-up, which runs every
`LAKANA_RUN__SWEEP_INTERVAL` seconds (an hour by default; `0` turns it off, and then nothing
expires). It drops at most `LAKANA_TABLES__MAX_EXPIRIES_PER_SWEEP` tables per clean-up (100 by
default), the most overdue first; the rest follow at the next one.

An expired table is dropped as if someone had dropped it: it disappears from the catalog, its
grants, row filters and column masks stop applying, and its files stay for the undo period
(`LAKANA_TABLES__UNDO_DAYS`, at least one day for an expiry even when it is 0). Restore it with
`lakana table undrop` as in [Drop and restore a table](drop-and-restore-a-table.md#restore-a-table).
A restored table counts as active from the restore on, so it is not dropped again at the next
clean-up; a day given with `--on` that has passed is removed by the restore.

## Through the APIs

- **REST.**
  - `GET` and `PUT /api/v1/environments/{env}/namespaces/{path}/tables/{name}/retention` show and
    set a table's retention. The body of `PUT` has exactly one of `{"days": 30}`,
    `{"on": "2026-12-31"}` and `{"never": true}`. The answer has `source` (`table`, `environment`
    or `null`), `days`, `on`, `never`, `set_by`, `set_at`, `last_activity_at`, `expires_at` and
    `kept` (`protected` or `project_asset` when a table with a retention never expires).
  - `PUT /api/v1/environments/{name}/retention` with `{"days": 30}`, or `{"days": null}` to turn it
    off; the environment shows `retention_days`.
  - `GET /api/v1/environments/{env}/expiring-tables?within_days=7` lists what expires soon.
  - Every table in `GET .../tables` and `GET .../tables/{name}` has `expires_at` and
    `expires_soon` (within the warning window).
- **SDK.** `client.tables.retention(env, "bronze.trial")`,
  `client.tables.set_retention(env, "bronze.trial", days=3)` (or `on=date(...)`, or `never=True`),
  `client.tables.expiring(env, within_days=7)` and `client.environments.set_retention(env, 30)`
  (`None` turns it off).

## If it does not work

| Message | Why | What to do |
|---|---|---|
| `Give exactly one of --days N, --on DATE or --never ...` | None or several rules were given. | Give one. |
| `The day a table expires on must be after today ...` | The day of `--on` is today or past. | Choose a later day, or drop the table. |
| `Environment 'prod' is protected, and its tables never expire, ...` | Protected environments keep their tables. | Nothing to do; `--never` removes a retention set before the protection. |
| `gold.v is not a managed table: only managed tables have a retention. ...` | Views, external tables and artifacts never expire. | Nothing to do. |
| A refusal (403) | Making a table expire sooner needs the right to drop it. | Ask its owner, or someone who may drop it. |
| `Created table dev/bronze.scratch, but could not set its retention: ...` | The table exists; the retention was refused. | Run the `lakana table retention` command the message names. |

## What the audit log records

- `TABLE_RETENTION_SET` for `--days` and `--on`, and `TABLE_RETENTION_CLEARED` for `--never`, by the
  person who changed it: `table_id`, `days`, `on`, `shortened` (whether the table now expires
  sooner), `previous` and `expires_at`.
- `ENVIRONMENT_RETENTION_SET` for an environment: `days` (empty when turned off) and
  `previous_days`.
- `TABLE_DROPPED` with `reason: retention` and no person when the worker drops an expired table, with
  `retention`: the rule's `source`, `days` or `on`, who set it, the table's last activity and when it
  expired.
- A refusal is recorded as `AUTHORIZATION_DENIED`, like every other.
