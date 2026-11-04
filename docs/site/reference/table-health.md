# Table health

What Lakana tells you about the state of a table without reading its rows: when it was last
written, how big it is, when it was last maintained, how its checks stand, who reads it, and what
its recent snapshots say. The numbers of the current snapshot are in `lakana table get`; the rest
is in `lakana table health`.

## The numbers of the current snapshot

`GET .../tables/{name}` and the table list return these fields next to the existing ones. They come
from the summary of the table's current snapshot, which the server has already loaded, so they cost
no storage read. A field the summary does not state is `null`; so are all four for a table with no
snapshot yet, a view, an external table and an artifact.

| Field | Type | Meaning |
|---|---|---|
| `last_write_at` | time or `null` | When the current snapshot was committed (UTC). |
| `row_count` | integer or `null` | Rows in the table now (the summary's `total-records`). |
| `size_bytes` | integer or `null` | Size of the table's data files (`total-files-size`). |
| `file_count` | integer or `null` | Number of data files (`total-data-files`). |

The summary is written by whoever committed the snapshot. A value that is not a whole number of 0 or
more is shown as `null`, not guessed.

## The health of a table

`GET /api/v1/environments/{env}/namespaces/{path}/tables/{name}/health?days=N` needs `READ_TABLE`
on the table, as the other table routes do; a caller without it gets `403`, a table that does not
exist `404`.

| Field | Type | Meaning |
|---|---|---|
| `days` | integer | The window `readers` covers. |
| `last_maintenance` | object or `null` | The last maintenance that changed the table; `null` if there is none. |
| `checks` | list | The newest result of each check of the table, by check name. Empty if it has none. |
| `readers` | list or `null` | Who read the table in the window; `null` if you may not see the table's grants. |

### `last_maintenance`

It is the newest maintenance run of the table, by the time it ended, that was not a dry run, did not
end on a conflict (nothing was applied) and did not fail. A run that ran past
`LAKANA_RUN__MAINTENANCE_TIMEOUT` and stopped at a safe point counts: it removed what its report says.
Nothing is stored apart from the run itself, so it is as old as the run record.

| Field | Type | Meaning |
|---|---|---|
| `run_id` | id | The query run that did it. |
| `finished_at` | time | When it ended. |
| `status` | `complete` or `stopped` | `stopped` means it ran past its time limit; running maintenance again continues where it stopped. |
| `snapshots_expired` | integer | Snapshots it expired. |
| `files_deleted` | integer | Orphaned files it deleted. |
| `bytes_deleted` | integer | Their total size. |
| `tags_removed` | integer | Tags of failed checks it removed. |

### `checks`

Each entry is the most recent result of one check, whichever run it was in. A check whose result
was removed with its run is not listed. The result of a generated reference check (`ref_<column>`)
whose target you may not read is not listed either; `hidden_checks` counts those, and
`lakana table health` says `1 check of a reference to a table you cannot read is not shown.`

| Field | Type | Meaning |
|---|---|---|
| `name`, `check` | text | `table/check` and the check's short name. |
| `run_id` | id | The run the result belongs to. |
| `outcome` | text | `passed`, `failed`, `warned` or `errored`. |
| `on_failure` | text | `block`, `keep` or `warn`. |
| `failing_rows` | integer or `null` | The count of rows that are wrong, when the check ran. |
| `error` | text or `null` | Why the check could not run. |
| `rollback_outcome` | text or `null` | What the rollback after a failed blocking check did. |
| `kept` | boolean | The check failed or errored and its `keep` setting left the data in the table. |
| `not_published` | boolean | The rollback left the table not published. |
| `finished_at` | time | When the check ended. |
| `generated` | boolean | The check was generated from the asset's header (`key:`, `references:`); see [Generated checks](checks.md#generated-checks). |

The marks are the ones the check history shows: `failed (kept)`, `not published`, and
`(generated)` after the name of a generated check. The data of a
check is a count, never a row.

### `readers`

Each entry is a principal that ran a query that read the table, over the last `days` UTC days (today
and the days before it).

| Field | Type | Meaning |
|---|---|---|
| `principal` | text | The principal's name. |
| `read_count` | integer | How many of its queries that succeeded named the table. |
| `last_read_at` | time | When the last of them completed. |

The list is ordered by `read_count`, most first, then by name.

**What counts as a read.** A query that succeeded adds one read, for its principal and the day it
completed, to each table or view its text names, however often it names it. It is counted in the
same transaction that completes the query, so a query that failed or was cancelled counts nothing.
The queries a run makes for you, such as a task's write and the count of a check, count as the run's
principal. A name that matches no table counts nothing.

**Python tasks.** A Python task reads each of its inputs through the query API, so those reads
count like any query's, under the run's principal.

**Gap.** A query that reads through a view counts on the
view only, not on the tables the view reads, so a table that people read only through views shows no
readers either.

**Who sees it.** The list shows who is using which data, so it is shown only to a caller who may see the table's grants: the
`ADMIN` role at instance scope, the same rule as the grants listing. For anyone else `readers` is
`null`; that is not an error, and the rest of the health is returned. An empty list means nobody read
the table in the window.

### How long reads are kept

| Setting | Default | Meaning |
|---|---|---|
| `LAKANA_TABLE_READS__KEEP_DAYS` | `90` | How many UTC days of reads are kept, today included (1 or more). A sweep that runs with the other sweeps (`LAKANA_RUN__SWEEP_INTERVAL`) deletes older counts. |
| `LAKANA_TABLE_READS__DEFAULT_DAYS` | `30` | The window `readers` covers when the request names none (1 or more, and not above the kept period). |

The server and the worker both read them; set them in the shared environment (see
[Deploy with Docker Compose](../how-to/deploy-with-compose.md#settings)). The server stops at start
with a message that names the setting if the default window is above the kept period.

`?days=` takes a whole number from 1 to the kept period. A number outside it is refused with `422`;
the message gives the range and names `LAKANA_TABLE_READS__KEEP_DAYS`. Lowering the kept period
deletes the older counts at the next sweep; raising it does not bring back counts already deleted.

## Signals

Every snapshot Lakana commits (a run, an append, a merge, a history write, a REST write) records
what it says about the table, from its summary and the column metrics Iceberg already holds. No
row is read. A table with signals shows them in `lakana table health`; a table whose first write
has not happened since signals were introduced shows none.

| Signal | Observed per snapshot | Fires when |
|---|---|---|
| `volume` | rows added by the snapshot and the table's total | the rows added fall outside the usual band |
| `null_rate` (per column) | null count divided by value count | the rate falls outside the usual band |
| `range` (per column) | lower and upper bound of the column | a bound lies beyond the learned minimum or maximum by more than the band |
| `schema` | the column list and types | a column is added, dropped or retyped |

**The band.** Over the table's last 30 snapshots, the median plus and minus 4 times the median
absolute deviation, widened by a floor so that small or very steady tables do not alert on noise:
at least 10 rows and 10 % for `volume`, 2 percentage points for `null_rate`, and for `range`, on
each side, 1 % of the learned span (for integers and other numbers, of the larger of the span and the bounds'
magnitude) and at least one unit: 1 for integers, 1 day for dates, 1 hour for timestamps, none for
other numbers. No signal but `schema` fires before the table has 8 snapshots.

**States.** `ok`: inside the band. `fired`: outside it (`alert` says how). `no_history`: fewer than
8 snapshots, so there is no band yet. `no_metrics`: nothing can be said, and the observation never
guesses; `observed.reason` tells why (`not_recorded`: Iceberg did not record the column's metrics,
`no_rows`, `unreadable`: the metrics are damaged, `not_finite`: a number too large to judge, `error`).

**Limits per commit.** Signals are computed for at most 200 columns, from at most 2,000 added
files, within 5 seconds. A commit over a limit records `no_metrics` for what it could not read; it
never fails the write.

### What `observed` and `band` hold

| Signal | `observed` | `band` |
|---|---|---|
| `volume` | `added`, `total` | `low`, `high`, `median` |
| `null_rate` | `nulls`, `values`, `rate` | `low`, `high`, `median` |
| `range` | `lower`, `upper`, `kind` | `min`, `max` (learned), `low`, `high` (the limits) |
| `schema` | `columns`, `added`, `dropped`, `retyped` | none |
| `no_metrics` (any) | `reason` | none |

`kind` of a `range` is `integer`, `number`, `date` (bounds as days since 1970-01-01) or
`timestamp` (bounds as microseconds since 1970-01-01 UTC).

### Fields of a signal

| Field | Type | Meaning |
|---|---|---|
| `id` | UUID | The observation. |
| `signal`, `column` | text, text or `null` | Which signal, and the column for `null_rate` and `range`. |
| `state` | text | One of the states above. |
| `effect` | text | The effect that applied; `warn` by default. |
| `snapshot_id` | integer or `null` | The snapshot observed. |
| `observed_at` | time | When it was recorded (UTC). |
| `observed`, `band` | object or `null` | As in the table above; `null` when hidden. |
| `alert` | text or `null` | The text of the newest firing (in `health`) or of this observation (in the history); `null` when it did not fire or its values are hidden. |
| `values_hidden` | boolean | `true` when `observed`, `band` and `alert` were left out for you. |
| `last_fired_at` | time or `null` | In `health` only: when this signal and column last fired. |

`GET .../tables/{name}/health` returns the current state of each signal in `signals`: the newest
observation of each signal and column.

### Who sees what

The signal routes need `READ_TABLE`, like the rest of the health, and a missing right is `403`.
Numbers can reveal values, so they are left out where a policy applies to you:

- **Masked column.** If a column mask applies to you, the signal's name, state, time and snapshot
  id are shown, but not `observed`, `band` or `alert` (`values_hidden` is `true`). A mask covers
  every name the column has had, so a rename does not lift it. When any mask applies to you,
  values under a name the current schema no longer has are left out too, and a table whose
  metadata cannot be read hides all values from you.
- **Row filter.** If a row filter applies to you, every value-revealing number (bounds, null
  rates, bands and alert values) is left out for all columns. `volume` and `schema` stay.
- **A masked column renamed since the mask was written.** All values are hidden, as the query
  path refuses such reads.
- **A kept table** (a failed check kept its data): the routes show aggregates and snapshot ids,
  never rows, current metadata or file locations.

### The history

`GET /api/v1/environments/{env}/namespaces/{path}/tables/{name}/health/signals` lists the
observations, newest first.

| Parameter | Meaning |
|---|---|
| `days` | How far back, from 1 to the kept period; 30 by default. A number outside the range is refused with `422` and the message names the range and `LAKANA_HEALTH__RETENTION_DAYS`. |
| `signal` | Only this signal. An unknown name is refused with `422`. |
| `column` | Only this column. |
| `limit` | Observations per page, 1 to 200 (100 by default). |
| `cursor` | The `next_cursor` of the previous page. |

The answer is `{"items": [...], "next_cursor": ...}`; `next_cursor` is `null` on the last page.
Signals are kept for `LAKANA_HEALTH__RETENTION_DAYS` days (90 by default, at least 7); the
maintenance sweep deletes older rows.

## Commands

| Command | What it does |
|---|---|
| `lakana table health NAME` | Shows the last maintenance, the newest result of each check and the readers of the default window. |
| `lakana table health NAME --days N` | The same with the readers of the last N days. |
| `lakana table health NAME --signals` | Lists the history of the health signals, newest first. Narrow it with `--signal S`, `--column C` and `--days N`; page with `--limit N` and `--cursor X`. |
| `lakana table health NAME --output json` | Every field of the answer as JSON. |
| `lakana table get NAME --output json` | Includes `last_write_at`, `row_count`, `size_bytes` and `file_count`. |

Without a right to see the grants the command says that the readers are not shown, and why.

## SDK

`client.tables.health(environment, "namespace.table", days=None)` returns a `TableHealth` with
`days`, `last_maintenance` (a `LastMaintenance` or `None`), `checks` (a list of `CheckHealth`) and
`readers` (a list of `Reader`, or `None`). `Table.last_write_at`, `Table.row_count`,
`Table.size_bytes` and `Table.file_count` hold the numbers of the snapshot; they are `None` for a
server that is older than table health.

`client.tables.health(...).signals` is a list of `HealthSignal` (empty from a server older than
signals). `client.tables.health_signals(environment, "namespace.table", days=None, signal=None,
column=None, limit=None, cursor=None)` returns one `HealthSignalPage` (`items`, `next_cursor`), and
`client.tables.iter_health_signals(...)` yields every observation across the pages.

## Audit

Seeing the readers of a table records who looked: when `readers` names anyone other than you, the
audit log gets an `ACTIVITY_VIEWED` event with `view` `table_readers`, the table, the `days` asked for
and how many readers were shown, never their names. It is written once per person and table per hour
(`LAKANA_VIEW_AUDIT__WINDOW_MINUTES`), so asking again or with other `days` adds nothing. Readers that
are only you, an empty list and `readers: null` record nothing. If the event cannot be written, the
request is refused with `503` and the message says so; try again in a moment, and if it keeps failing
ask the operator to check the server's database. The event and its fields:
[Runs, queries and deploys lists](lists.md#audit).

The rest of the health is not audited; a refusal is recorded as `AUTHORIZATION_DENIED`, as for any
table route.
