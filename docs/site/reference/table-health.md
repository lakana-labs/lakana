# Table health

What Lakana tells you about the state of a table without reading its rows: when it was last
written, how big it is, when it was last maintained, how its checks stand, who reads it, and what
its recent snapshots say, and what to do when they say something is wrong. The numbers of the current snapshot are in `lakana table get`; the rest
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
row is read. Signals are recorded when a commit adds a snapshot and makes it current: a commit that
moves `main` to a snapshot that already exists (a fast-forward of a branch, say) adds no snapshot
and records none. A table with signals shows them in `lakana table health`; a table whose first
write has not happened since signals were introduced shows none. Views, external tables and
artifacts have no signals.

When a failed check sets a table back, the signals of the snapshots it set back are no longer the
table's data: they neither show as its current state nor count towards the usual band. The history
keeps them, marked `set_back` (`(set back)` after the state in `lakana table health --signals`), so
the firing that made a blocking signal set the table back stays there to look at, as long as the
other signals are kept. A first build that is left not published
keeps no signals: nobody may read its data.

| Signal | Observed per snapshot | Fires when |
|---|---|---|
| `freshness` | time since the last snapshot that added data, checked hourly | it is longer than the schedule of the job that builds the table allows, or than its own history |
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
`no_rows`, `unreadable`: the added files' manifests could not be read, or were too many or too
slow to read within the limits below, `not_finite`: a number too large to judge, `error`). Metrics
that are damaged count as `not_recorded`.

**Limits per commit.** Signals are computed for the first 200 top-level columns, from at most
2,000 added files, within 5 seconds. Columns past the 200th record nothing. A commit over the file
or time limit records `no_metrics` with reason `unreadable` for `null_rate` and `range`; it never
fails the write.

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
| `set_back` | boolean | `true` when a failed check set the observed snapshot back; such a row shows only in the history. |

`GET .../tables/{name}/health` returns the current state of each signal in `signals`: the newest
observation of each signal and column.

### Who sees what

The signal routes need `READ_TABLE`, like the rest of the health, and a missing right is `403`.
Numbers can reveal values, so they are left out where a policy applies to you. `values_hidden`
then tells you, per column, that values are hidden for you because a column mask or row filter
applies to you (the missing values would show that anyway); it never says which policy, nor its
definition. A policy that does not apply to you changes nothing you see.

- **Masked column.** If a column mask applies to you, the signal's name, state, time and snapshot
  id are shown, but not `observed`, `band` or `alert` (`values_hidden` is `true`). A mask covers
  every name the column has had, so a rename does not lift it. When any mask applies to you,
  values under a name the current schema no longer has are left out too, and a table whose
  metadata cannot be read hides all values from you.
- **Row filter.** If a row filter applies to you, every value-revealing number (bounds, null
  rates, bands and alert values) is left out for all columns. `volume`, `freshness` and `schema`
  stay: `last_write_at` shows when the table was last written anyway.
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
`LAKANA_HEALTH__RETENTION_DAYS` (90 by default, at least 7) is how far back `days` may reach. The
workers' hourly sweep (`LAKANA_RUN__SWEEP_INTERVAL`) deletes older observations, but keeps the
newest of each signal and column, so the current state never goes, and the first commit of
each of the table's last 31 writes that added data (see below for what one write is), so freshness keeps the history it is judged from however long ago they were. Each sweep handles at most
500 tables and the next sweep the next 500, so with more tables the deletion takes several hours
to come round. A message still waiting to be delivered when its signal is deleted is dropped with
it.

## Effects: warn, block, off

A fired signal warns by default: it is recorded, shown, and sent to the jobs that build the table
(see [Messages to the jobs that build the table](#notifications)). You
choose, per signal, between three effects:

| Effect | What it does |
|---|---|
| `warn` | The default. The signal is recorded, shown and sent to the building jobs' `notify:` when it fires. |
| `block` | A firing signal fails the run and sets the table back, as a blocking check does. |
| `off` | The signal is not evaluated for the table or column, and shows as `off`. |

**When a signal blocks.** Each `block` entry of a table adds one check to the table's task, named
`health_<signal>` (`health_range__price` for one column), marked generated like the checks of the
asset's header; a deploy refuses a hand-written check of the table with that name
([Names](checks.md#generated-checks)). It reads no rows: after the write, it looks at whether the
signal fired at any snapshot the run wrote (a Python asset that commits several times has every
commit judged, not only the last). When a failed attempt is tried again, every commit of the run
is judged for an `append`, `merge` or `history` write, since an earlier attempt's commits stay in
the table; for a write that replaces the table only the commits of the attempt that wrote last
are, since it replaced what the failed one had committed. `null_rate: block` or `range: block`
fails when any column whose effect is `block`
fired. When the signal fired, the check fails, the table is set back, and nothing downstream is
built, as for any failed blocking check. The run's failure message, the check's line in
`lakana runs show` and the task's log (`lakana runs logs`) give the check's text, which names no
value, because everyone who may read the run reads it:

> Check `gold.orders/health_volume` failed: health signal volume fired at snapshot 4711; `lakana table health gold.orders --env prod --signals` shows its usual band and new value to readers who may see them; fix the data the asset reads, or let such data through with health: {volume: warn} in the asset header (assets/gold/orders.sql).

The history shows the band and the value to those a mask or filter does not hide them from (see
[Who sees what](#who-sees-what)). `lakana check rows` of such a check says that it reads no rows and
names the same command. A first build that is set back is not published and keeps no signals, so
its text says so instead of naming the history:
`… fired at snapshot 4711; this first build is not published, so its health signals are not kept;
fix the data the asset reads, or let such data through with health: {volume: warn} in the asset
header`. A signal that fired without `block` never stops a run, and a signal without enough history
yet (`no_history`) never blocks. A view or an artifact never gets such a check.

**When a signal could not be judged.** A `block` signal that a commit of the run could not judge
does not pass in silence: the check is recorded as `warned` (and with `on_failure` `warn`), the run
goes on and the table is not set back. That is the case when the commit's files could not be read
within the health limits (more than 2,000 added files, or longer than 5 s), when computing the
signals failed, or when nothing was recorded for the signal or the check's column. The check's text
says why and what to do, and names no value:

> 1 check warned: `health_volume` (health signal volume could not be judged: computing it failed (snapshot 4711); the run went on: the server's log names the error, and the next commit is judged again).

It is in the run's message, `lakana runs show` and the task's log, and the notify targets of the
jobs that build the table get a `health` message with the same text when the commit recorded why
(`no_metrics` with reason `unreadable` or `error`), once until the signal is judged again. A column
that never records a value can never be judged: `range` of a column without a range (text,
boolean, a nested type) and any column past the 200th. A deploy that knows the columns refuses
`block` on a column past the 200th; for the rest, `lakana table get` marks the entry, as in
`range of note block (never judged: a string column has no range)`. Use `null_rate` for such a
column, or set it to `warn` or `off`. Which
signals block comes from the deployment the run started with; for `null_rate: block` and
`range: block`, which columns block comes from the effects of the current deployment, so a deploy
that lands while the run is going decides those. Such a deploy that sets a blocking signal `off`
also lifts its block for the commits after it: they record the signal as `off`, which never fires.

Write `health:` in the asset's `/* lakana … */` block, or as `health=` in `@asset(...)`: `off` for
every signal, or a mapping of signals to an effect. `null_rate` and `range` may instead name single
columns.

The header, with an example, is in [Asset front-matter](asset-front-matter.md#health-what-the-tables-signals-do);
[Notice a broken pipeline](../how-to/notice-a-broken-pipeline.md) walks through a blocked run.

A project default goes in [`lakana.yaml`](lakana-yaml.md#health) under `health:`; the asset's own
entries win over it. `freshness` cannot `block`: it fires between runs, when there is no write to
stop. A view, an external table and an artifact have no snapshots, so they take no `health:`.
Write `off` unquoted or quoted; `on`, `yes` and `true` are not effects. A deploy refuses a bad
`health:` with the file, the cause and the next step:

| Text | Cause |
|---|---|
| ``health: rows is not a health signal; use freshness, volume, null_rate, range or schema`` | A signal that does not exist. |
| ``health: volume must be warn, block or off, not loud`` | Any other effect. |
| ``health: freshness cannot block: it fires between runs, when there is no write to stop; use warn or off`` | `block` on `freshness`. |
| ``health: volume is about the whole table; give it warn, block or off, not columns`` | Columns under a signal that is not per column. |
| ``health: null_rate takes warn, block or off in lakana.yaml; name single columns in an asset's header`` | Columns in `lakana.yaml`. |
| ``health: range names column price, which gold.orders does not have (columns: order_id); fix the name or remove it`` | A column the table's `SELECT` does not return. |
| ``health: does not apply to a view, which has no snapshots to watch; remove it, or materialize the asset as a table`` | `health:` on a view. |
| ``health: range of c200 can never block: only the first 200 columns of a table are judged, and c200 is column 201; move it among the first 200, or set it to warn or off`` | `block` on a column past the 200th of the `SELECT`. |

The deploy checks a column only when it knows the asset's columns: from a SQL asset's column
list. For a `SELECT *` and for a Python asset it cannot; an entry for a column the table does not
have then has no effect, and `lakana table get` marks it, as in
`range of pricee off (no such column)`.

The effects apply to a managed table from the next deploy on, or from its creation when a run
creates it after the deploy. A table that no asset builds, whose asset a deploy removed, or whose
repository was undeployed uses the defaults: every signal warns. `lakana table get NAME` lists
every effect that is not the default `warn`, in the order of its keys, as in
`Health (not the default warn): range of price off, volume block`.

A project in which a table declares `health:` (in the asset or as a default in `lakana.yaml`)
deploys manifest version 14. Upgrade the workers before you deploy it: a worker of an earlier
release refuses that manifest. A project without `health:` keeps its version.

## Freshness

Freshness is judged between writes, so there is no commit to do it at: the workers check it hourly, with the other sweeps (`LAKANA_RUN__SWEEP_INTERVAL`),
and record a new observation only when its state or the last snapshot changes. It counts only
snapshots that added data: a compaction does not make a stale table fresh. A table built by a
scheduled job is late after twice the interval of that schedule, and never before the second run
after its last snapshot has passed, so a weekday job is not late over the weekend. With several
such jobs, the shortest limit counts. A table without one is late after 3 times the median gap
between its writes, and at least 1 hour; a schedule that cannot be read counts as none. For that
median, the snapshots within 10 minutes of a write's first snapshot count as one write: a run that
commits three times is one, so a table written in bursts is not late an hour after each run, and a
table written every 5 minutes has a write every 10, so it is late after an hour's stall. It reads the last 31 writes,
from at most the last 1,000 snapshots that added data, and with fewer than 8 writes the table
shows `no_history`. A table whose
building jobs are all scheduled and paused shows `off` with `observed.reason` `paused`: it is not
expected to change, and nothing is sent. When a job without a schedule builds it too (one that CI
or another tool starts), that job can write at any time, so the table is judged by its history.

The sweep handles at most 500 tables per run and the next run the next 500, so with more
tables a table's freshness is judged every few hours. A table that has not been written since the
upgrade to the release that introduced signals has no freshness until its next write. `freshness: off`
switches it off, and it cannot `block`: it fires between runs, when there is no write to stop.

## Notifications

A fired signal whose effect is `warn` is sent to the jobs that build the table: every job of the
environment's current deployments whose selection holds it and that declares `notify:` (see
[Get told when a job fails](../how-to/get-told-when-a-job-fails.md)). Each job gets one message
with the event `health`, through its own webhook and format. A table that no job with `notify:`
builds is only shown.

- **Once per firing.** A signal that keeps firing commit after commit sends one message, not one
  per run; it sends again only after an observation of the same signal and column that did not
  fire. A firing whose snapshot a check set back does not count. A firing that no job is told of,
  because no job with `notify:` builds the table at that moment, still counts as the last one: a job
  that gets `notify:` later hears of the signal only after it has been `ok` once.
- **`block`.** A firing that a run's own `health_<signal>` check judges fails the run, and the
  run's `failed` message names the check, so no `health` message is sent for it. A `block` signal
  that fires without such a check, in a write outside a run or in a run that started before the
  deploy that set `block`, is sent like a `warn` one.
- **What it says.** The title reads ``Lakana: table `gold.orders` needs a look in `prod` ``, then
  the signal's alert, the job and repository, the command `lakana table health TABLE --env ENV`
  and the delivery id. In `json` the event is `health`, `run_id`, `status`, `commit` and `trigger`
  are empty, and two keys are added: `table` and `alert`.
- **Values.** Whoever reads the channel is outside Lakana: nobody can ask whether a policy applies
  to them. For a table with any row filter, and for a `null_rate` or `range` signal of a table
  with any column mask, the alert names only the table, the signal, the column, the snapshot, the
  time and the next step, never the usual band or the new value. The message does not say why.
  Without a policy it is the signal's `alert` (see [Fields of a signal](#fields-of-a-signal)),
  with the band and the value.
- **Set back later.** A message already queued is still sent when a later check sets the
  firing's snapshot back: it tells of a firing that happened.

The alert of a `null_rate` signal of a table with a column mask reads:

> gold.orders: health signal null_rate of email fired at snapshot 4711 (03 Nov 06:00 UTC); `lakana table health gold.orders --env prod --signals` shows its usual band and new value to readers who may see them; check what fills email upstream, or set health: {null_rate: {email: off}} if this is expected

## How long signals are kept

| Setting | Default | Meaning |
|---|---|---|
| `LAKANA_HEALTH__RETENTION_DAYS` | `90` | How many days of observations are kept (7 or more); `days` of the history may reach this far. |

The workers' hourly sweep (`LAKANA_RUN__SWEEP_INTERVAL`) deletes older observations, but keeps the
newest of each signal and column, so the current state never goes. How the sweep treats freshness
history and what happens to a message still waiting is in [The history](#the-history).

## Commands

| Command | What it does |
|---|---|
| `lakana table health NAME` | Shows the last maintenance, the newest result of each check, the readers of the default window and the current state of each health signal, with the alerts that fired. A table without signals says why: a managed table gets them with its next write, a view, an external table or an artifact has none, and a server older than health signals does not report them (upgrade it). |
| `lakana table health NAME --days N` | The same with the readers of the last N days. |
| `lakana table health NAME --signals` | Lists the history of the health signals, newest first; a signal whose snapshot a failed check set back has `(set back)` after its state. Narrow it with `--signal S`, `--column C` and `--days N`; page with `--limit N` and `--cursor X`. |
| `lakana table health NAME --output json` | Every field of the answer as JSON. |
| `lakana table get NAME` | Lists the effects of `health:` that are not the default `warn`, and marks one that names a column the table does not have. |
| `lakana table get NAME --output json` | Includes `last_write_at`, `row_count`, `size_bytes`, `file_count`, `health` (the effects that are not `warn`, by `signal` or `signal.column`), `health_missing` (the keys of `health` that name a column the table does not have) and `health_never_judged` (the `block` column entries no commit can judge, with the reason). |

Without a right to see the grants the command says that the readers are not shown, and why.

## SDK

`client.tables.health(environment, "namespace.table", days=None)` returns a `TableHealth` with
`days`, `last_maintenance` (a `LastMaintenance` or `None`), `checks` (a list of `CheckHealth`) and
`readers` (a list of `Reader`, or `None`). `Table.last_write_at`, `Table.row_count`,
`Table.size_bytes` and `Table.file_count` hold the numbers of the snapshot; they are `None` for a
server that is older than table health. `Table.health` holds the effects that are not `warn` (empty when
every signal warns, and from an older server), `Table.health_missing` the keys of it that name a
column the table does not have, and `Table.health_never_judged` the `block` column entries that no
commit can judge, with the reason (empty from an older server).

`client.tables.health(...).signals` is a list of `HealthSignal`, or `None` from a server older
than signals. `client.tables.health_signals(environment, "namespace.table", days=None, signal=None,
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
