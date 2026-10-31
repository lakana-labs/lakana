# Check your data

After this guide you can state in SQL what must be true of a table, have Lakana test it every time
the table is built, and you know what happens to the table when the test fails. You need a
project that you have deployed once (see [Getting started](../tutorials/getting-started.md)).

The guide works on one small project, the example `docs/examples/check-your-data/` of the Lakana
repository. Copy its `project/` folder into a new folder, or run `lakana init --no-sample` and add the files
that each step shows; every file the guide names is shown in full.

In short:

- A **check** is a SQL file that returns the rows that are wrong. No rows, the check passes.
- A check that fails **sets the table back to what it was before the run**, and the tables that
  are built from it are not built. A table that this run built for the first time is left *not
  published* instead, because there is nothing to set it back to.
- A check can instead **keep** the failing data: the table holds what the run wrote, nothing
  that reads it is built, and that stays so until its checks pass. See
  [Block, keep or warn](#block-keep-or-warn).
- **Readers can see the failing data in between.** A run writes the table first and checks it
  afterwards, so for as long as the checks take, anyone who reads the table sees the data that is
  about to be set back. If that is not acceptable for a table, a check alone is not enough for it:
  see [Limits](#limits).

## Adding checks to a pipeline that already runs

A new check stops a pipeline that already runs the first time it finds something, and what it
finds is often a fault that has been there for months. To add checks without stopping anything,
give the project a default of `warn` and tighten it check by check:

1. Add this to `lakana.yaml`:

<!-- source: docs/examples/check-your-data/default/checks.yaml -->
```yaml
checks:
  on_failure: warn
```

2. Write your checks and deploy. A check without an `on_failure` of its own takes the default, and
   the deploy says how many did; [A default for the whole project](#a-default-for-the-whole-project)
   shows the output.
3. Read the results of a few runs, as [See what failed](#see-what-failed) explains. A check that
   warns would have stopped the pipeline if it had blocked; nothing has stopped.
4. Give each check the `on_failure` it should have: `block` for data that must never reach
   readers, `keep` for data you want to look at in the table, `warn` for a fault worth knowing
   about. A check with its own `on_failure` ignores the default. See
   [Block, keep or warn](#block-keep-or-warn).
5. When every check has its own value, remove the default or set it to `block`.

The default takes `block` or `warn`, and an environment can override it (see
[`lakana.yaml`](../reference/lakana-yaml.md#the-default-of-the-checks)). It does not take `keep`: `keep` leaves
failing data where readers find it, so you choose it check by check. A deploy that sets it
anyway is refused with this message, which names the way out:

``checks.on_failure: must be block or warn (got 'keep'). `keep` leaves failing data where readers find it, so it can only be set on a check: put `on_failure: keep` in that check's /* lakana */ block.``

## Write a check

A check is a file under `checks/`. The directory names the table, the file names the check. The
checks of the project in this guide, as `find` lists them:

<!-- source: docs/examples/check-your-data/run.sh#list-checks -->
```bash
find checks -name '*.sql' | sort
```

<!-- output: check-your-data#list-checks -->
```text
checks/silver/station_temperatures/has_rows.sql
checks/silver/station_temperatures/no_missing_station.sql
checks/silver/station_temperatures/plausible_range.sql
```

The directory has the form `checks/NAMESPACE/TABLE/NAME.sql`, so these are the checks of the table
`silver.station_temperatures`.

The check's full name is `silver.station_temperatures/no_missing_station`: the table, a slash, the
file name without `.sql`. The table must be one that an asset of your project builds, or reads
(see [A table your project only reads](#a-table-your-project-only-reads)).

A file holds one `SELECT` that returns the rows that are **wrong**. This one returns the averages
that have no station:

<!-- source: docs/examples/check-your-data/project/checks/silver/station_temperatures/no_missing_station.sql -->
```sql
/* lakana
description: Every average belongs to a station.
*/
SELECT station_id, avg_temp_c
FROM silver.station_temperatures
WHERE station_id IS NULL
```

The comment at the top is optional. It may hold a `description` and, as
[Block, keep or warn](#block-keep-or-warn) explains, `on_failure`. The rest of the file is the `SELECT`. It
must read the table that its directory names. Lakana counts the rows it returns; the columns you
select are for you, when you run the check yourself to see what is wrong.

`lakana init` writes one check for its sample, `checks/silver/station_temperatures/plausible_range.sql`.
If you delete the sample asset `assets/silver/station_temperatures.sql`, delete its check too,
or the deploy is refused because the check belongs to a table that your project no longer builds.
`lakana init --no-sample` writes neither.

The rest of this guide uses a project of two assets. The first writes six readings, one of them
without a station and one of 999 degrees:

<!-- source: docs/examples/check-your-data/project/assets/bronze/readings.sql -->
```sql
/* lakana
description: Six temperature readings from three weather stations, written by hand.
columns:
  station_id: The station that took the reading.
  temp_c: The temperature in degrees Celsius.
*/
SELECT 'A1' AS station_id, 4.0 AS temp_c
UNION ALL SELECT 'A1', 6.0
UNION ALL SELECT 'B2', 1.0
UNION ALL SELECT 'B2', 3.0
UNION ALL SELECT NULL, 2.0
UNION ALL SELECT 'C3', 999.0
```

The second averages them for each station:

<!-- source: docs/examples/check-your-data/project/assets/silver/station_temperatures.sql -->
```sql
/* lakana
description: The average temperature of each station.
columns:
  station_id: The station that took the readings.
  avg_temp_c: The average temperature in degrees Celsius.
*/
SELECT station_id, AVG(temp_c) AS avg_temp_c
FROM bronze.readings
GROUP BY station_id
```

Besides `no_missing_station`, the table has two more checks. `plausible_range` only warns, and
`has_rows` passes. Both are under [Five checks to copy](#five-checks-to-copy) and
[Block, keep or warn](#block-keep-or-warn). Two more assets, with a check that keeps,
come later in the same section.

## Deploy, and see which checks a project has

A deploy reads `checks/` together with `assets/` and refuses a check it cannot run, with the file
and line. A dry run does the same and lists the checks it found, a table at a time:

<!-- source: docs/examples/check-your-data/run.sh#deploy-dry-run -->
```bash
lakana deploy --env dev --dry-run
```

<!-- output: check-your-data#deploy-dry-run -->
```text
Queued dry run 5c3be0a7-eead-4c62-bd70-eaad2b3bb1b2
Dry run of readings in dev at 27083bdc2e1a95cd44bccffbc705282d7c99e59d
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ change ┃ table                       ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ added  │ bronze.readings             │
│ added  │ silver.station_temperatures │
└────────┴─────────────────────────────┘
Checks:
  silver.station_temperatures: has_rows, no_missing_station, plausible_range (warn)
```

Under `Checks:` each table is followed by its checks; `(warn)` marks one that only warns. Every
refusal a deploy can give for a check, and what to do about it, is in
[Checks](../reference/checks.md#what-a-deploy-refuses). Deploy for real by leaving out
`--dry-run`.

## Run, and see a check fail

Run the project:

<!-- source: docs/examples/check-your-data/run.sh#run-failing -->
```bash
lakana run --env dev
```

The command prints a line each time a task changes state, and ends with a summary. The run failed,
so the command exits with status 1. The summary of this run, on a terminal 120 columns wide:

<!-- output -->
```text
Run 941ef675-0bb6-497a-8bd5-8bfd22d7d2ae failed
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ asset                       ┃ status    ┃ attempt ┃ error                                                            ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ bronze.readings             │ succeeded │ 1/3     │                                                                  │
│ silver.station_temperatures │ failed    │ 1/3     │ Check `silver.station_temperatures/no_missing_station` failed: 1 │
│                             │           │         │ row (checks/silver/station_temperatures/no_missing_station.sql). │
│                             │           │         │ The table is not published: it had no published data before this │
│                             │           │         │ run. Nothing downstream was built. Look at the rows: `lakana     │
│                             │           │         │ check rows 941ef675-0bb6-497a-8bd5-8bfd22d7d2ae                  │
│                             │           │         │ silver.station_temperatures/no_missing_station --env dev`. After │
│                             │           │         │ a fix, deploy and run again; if the project did not change:      │
│                             │           │         │ `lakana runs resume 941ef675-0bb6-497a-8bd5-8bfd22d7d2ae --env   │
│                             │           │         │ dev`.                                                            │
└─────────────────────────────┴───────────┴─────────┴──────────────────────────────────────────────────────────────────┘
Not shown: duration, rows. Use --output json to see them.

silver.station_temperatures   FAILED   2 of 3 checks passed, not published
  has_rows                    passed
  no_missing_station          FAILED   1 row
  plausible_range             warned   1 row
```

Read it from the top:

- `bronze.readings` succeeded.
- `silver.station_temperatures` failed. Its message names the check, how many rows it found, the
  file of the check, what became of the table and what to do next.
- Below the table, the checks of that task. `FAILED` is a blocking check that found rows.
  `warned` is a check that only warns, and `passed` found none. `2 of 3 checks passed` counts the
  warning as passed, because it did not stop anything.

## What a failed check does

**To the table.** The table is set back to the data it held before this run, in one step, and the
message says so. What the message says depends on what the run had done to the table:

| The message says | It happens when |
|---|---|
| `The table was set back to what it was before this run, and nothing downstream was built.` | The table held data before the run, and the run replaced it or added to it. This is the usual case. A change to the table's columns that the run made goes back with it, too. |
| `The table is not published: it had no published data before this run. Nothing downstream was built.` | The run built the table for the first time, so there is nothing to set it back to. See [A table that is not published](#a-table-that-is-not-published). |
| `The table was not changed by this run, and nothing downstream was built.` | The run wrote no rows (an incremental asset found nothing new), so there was nothing to undo. |
| `It is a view, so there is nothing to set back; nothing downstream was built.` | The asset is a view. A view holds no data of its own; its rows are its sources' rows. |
| `The table was left as it is, because another writer changed it while this run was working on it; it may hold data of this run that failed its checks. Nothing downstream was built.` | Somebody else wrote to the table between the run's write and its set-back. Lakana never undoes another writer's work, so it changes nothing. Look at the table and decide. |
| `... The table was set back to before this task ran.` | The worker running the task was lost or stopped for memory after it may have written its table, or you cancelled the run while the table held rows no check had passed, and the platform set the table back (see [A worker that is lost](#a-worker-that-is-lost)). The text before it says why the task ended: the message of a lost worker, the message of the memory stop, `The run was cancelled.` for a run you cancelled, or `The run was stopped because another task failed its code integrity check.` for a run the platform stopped for that reason. |
| `... The task had written nothing to its table, so nothing was set back.` | The same, but the table is as it was before the task: the task had not written to it, or what it wrote was set back already. |
| `... The table changed after this task wrote it, so it was not set back and may still hold rows no check has passed. Run again, which rebuilds the table, or fix the data by hand.` | The same, but somebody else wrote to the table after the task did. Lakana never undoes another writer's work, so it changes nothing. Run again, or look at the table and fix it. |
| `The table could not be set back: the worker was lost twice while setting it back. The table may still hold rows no check has passed. Run again, which rebuilds the table, or fix the data by hand.` | Two workers in a row were lost while they set the table back. The table is not set back. Run again, which builds it again, or fix the data by hand. |
| `The rollback failed (REASON), so the table still holds this run's data. Nothing downstream was built.` | The set-back itself did not work: the reason says why (a storage or database error, or the earlier state was removed). Table maintenance keeps the snapshot that a run still in progress needs to set its table back, whatever its retention settings say. The table still holds the failing data. Run again once the cause is gone, or fix the data by hand. |

The outcomes have short names that you meet in `lakana check list --output json` and in the
reference: `rolled_back`, `unpublished`, `nothing_written`, `not_a_table`, `changed_since` and
`failed`, in the order of the table. In this guide the table is always "set back", and a group in
`lakana runs show` reads `rolled back` for the first of them.

Two messages are rarer. If the database was interrupted during the set-back and Lakana could not
find out whether it took effect, the message says so and names `lakana table get`; look at the
table. If the platform refused the set-back, the message gives the reason and says that the table
was not changed.

**To the run.** The tables that read the failed table are not built; they end `upstream_failed`.
Tasks that do not depend on the failed table go on, and the run ends `failed`. Every check of the
table runs, also after one has failed, so one run tells you everything that is wrong with it.

**To the rows.** A set-back takes the failing rows out of the table. `lakana check rows` still
shows them, as they were in that run: see [Look at the rows that
failed](#look-at-the-rows-that-failed).

A check that cannot run (its SQL fails, it takes longer than the time limit of a query, it runs
out of memory) counts as failed, and a blocking check that cannot run sets the table back, too:
data that could not be checked is not published. The check's result says that it could not run
and why, in words that never quote a value from your table.

A temporary failure is different: if the object store or the database could not be reached while a
check was counted, the task is tried again on its next attempt, as every task is (you see `attempt
2/3` and the wait reason `retry`, see [Why is my query or task
waiting?](why-is-it-waiting.md#a-waiting-task)). The table is not built again; only the checks run
again. Meanwhile the table holds the run's data, not yet checked. Only if the last attempt fails
the same way does the check count as one that could not run.

### A worker that is lost

A worker can be lost (killed, or cut off from the database) after a task wrote its table and
before the checks passed. The table then holds rows no check has passed, so the platform does not
end the task at once: it keeps the task unfinished and the next free worker sets the table back,
and only that. The task shows the wait reason `setting_back` (see [Why is my query or task
waiting?](why-is-it-waiting.md#a-waiting-task)) and then ends. While the worker does it,
`lakana runs show` prints `setting the table back` in the task's status column. The task ends:

- `failed`, when it was lost on its last attempt, with the message of the lost worker and then
  what became of the table (the rows of the message table above);
- `cancelled`, when you cancel the run, with `The run was cancelled.` and then the same
  sentence. A run that was stopped because another task failed its code integrity check ends its
  tasks the same way, with `The run was stopped because another task failed its code integrity
  check.` in place of the first sentence.

This holds for a Python asset with checks lost after its last attempt's write, and, in a run that
you cancel, for any asset with checks that may have left unchecked rows. It holds as well when the
worker is not lost but ends the task itself: a worker that sees your cancel after its task wrote
its table, and a worker that stops for using too much memory (below) hand the task back in the
same way, and the next free worker sets the table back before the task ends. The cancel can come
while the worker is lost or later, when the task already waits again: for its checks after a lost
worker (wait reason `retry`, or `no_worker` when that worker was the only one), or for the next
attempt of a Python asset with checks whose earlier attempt may have written. Instead of ending
such a task, the cancel sends it to set the table back first, so the run ends only once a
pipeline worker has done that: with no worker running, the cancelled run waits with the reason
`no_worker` until you start one. In a run that is not cancelled, a
task with attempts left runs again as before, and its own set-back covers a failure. A worker that
is lost while it sets the table back is replaced once more; after two such workers the task ends
with the last message of the table above. A worker that is stopped on purpose (a drain) while it
sets the table back hands the task over with the flag kept, and that does not count against the
two. Setting the table back is a rollback to the state before the task ran, so it does not run
any check, and a Python asset that writes through the platform is set back to where it started.

One case is not covered: a Python asset without checks records no baseline, so nothing can be
set back for it: see [Python assets](#python-assets).

A worker that stops for using too much memory ends its task like this when the stop ends the task
for good and the task may have written: a Python asset with checks, on its last attempt, or on any
attempt when the worker was running only that task (always on macOS; on Linux, while a later
attempt builds its environment after an earlier attempt wrote). The task ends `failed` with the
message of the memory stop and then what became of the table. A stop that leaves attempts starts
the next attempt from the baseline, as before, and hands nothing back.

## A table that is not published

A table that failed its first build stays registered, with its columns, its grants and its
policies, but a read of it is refused, because an empty table would look like "no data yet":

<!-- source: docs/examples/check-your-data/run.sh#read-unpublished -->
```bash
lakana sql "SELECT count(*) FROM silver.station_temperatures"
```

<!-- output: check-your-data#read-unpublished -->
```text
Error: `silver.station_temperatures` has not been published: its first build failed the check `no_missing_station` (run
941ef675-0bb6-497a-8bd5-8bfd22d7d2ae). Look at the rows: `lakana check rows 941ef675-0bb6-497a-8bd5-8bfd22d7d2ae
silver.station_temperatures/no_missing_station --env dev`. It is published by the next run in which its checks pass.
```

`lakana table get` and `lakana table list` show the state in a column named `state`. (The
`metadata_location` row of `table get` is left out below; it holds a path of your machine.)

<!-- source: docs/examples/check-your-data/run.sh#table-get -->
```bash
lakana table get silver.station_temperatures --env dev
```

<!-- output -->
```text
┌───────────────────┬──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ name              │ station_temperatures                                                                             │
│ namespace         │ silver                                                                                           │
│ kind              │ TABLE                                                                                            │
│ state             │ not published                                                                                    │
│ uuid              │ 57b357f1-0228-44a2-b704-f346e298c63e                                                             │
└───────────────────┴──────────────────────────────────────────────────────────────────────────────────────────────────┘
`silver.station_temperatures` has not been published: its first build failed the check `no_missing_station` (run
941ef675-0bb6-497a-8bd5-8bfd22d7d2ae). Look at the rows: `lakana check rows 941ef675-0bb6-497a-8bd5-8bfd22d7d2ae
silver.station_temperatures/no_missing_station --env dev`. It is published by the next run in which its checks pass.
```

The next run in which the table's checks pass publishes it, and `state` reads `published` again.
You can set grants, row filters and column masks on the table meanwhile, and `lakana table delete`
removes it as it removes any table.

## See what failed

Three commands show the result of a run's checks, with the same information:

- `lakana runs show RUN_ID --env dev` prints the run, its tasks and, at the end, the checks of
  every task, as in the summary above.
- `lakana check list --env dev --run RUN_ID` lists them as a table, one row for each check, with
  its outcome and the number of rows it found. Without `--run` it lists the newest results of the
  environment, and `--table silver.station_temperatures` keeps one table's.
- `lakana runs logs RUN_ID --env dev` prints the log of each task, with its checks and its message
  in full.

`RUN_ID` is the identifier on the first line of the run's output, and `lakana runs list --env dev`
lists the latest runs. Both print the identifier whole; the `run` column of `lakana check list`
shows only its first eight characters. In the command below, `$RUN_ID` stands for that identifier; type the
identifier itself. The checks of this run:

<!-- source: docs/examples/check-your-data/run.sh#check-list -->
```bash
lakana check list --env dev --run "$RUN_ID"
```

<!-- output -->
```text
┏━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ run      ┃ table                       ┃ check              ┃ outcome ┃ rows ┃ finished_at               ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 941ef675 │ silver.station_temperatures │ has_rows           │ passed  │ 0    │ 2026-10-11T16:23:31+00:00 │
│ 941ef675 │ silver.station_temperatures │ no_missing_station │ failed  │ 1    │ 2026-10-11T16:23:31+00:00 │
│ 941ef675 │ silver.station_temperatures │ plausible_range    │ warned  │ 1    │ 2026-10-11T16:23:32+00:00 │
└──────────┴─────────────────────────────┴────────────────────┴─────────┴──────┴───────────────────────────┘
Not shown: on_failure. Use --output json to see it.
```

The result holds **how many** rows a check found, and the check's file, but not the rows
themselves. [Look at the rows that failed](#look-at-the-rows-that-failed) shows how to see them.

## Look at the rows that failed

`lakana check rows RUN_ID CHECK --env dev` shows the rows a check found in a run. `RUN_ID` is the
whole identifier of the run, as the first line of the run's output and `lakana runs list --env dev`
print it; the eight characters that `lakana check list` shows are not enough. `CHECK` is the
check's name as `lakana check list` shows it: the table, a `/` and the check. The message of a
failed task names this command with both filled in, so you can paste it: a name with a space or a
quote in it is already in quotes there. A name that a message cannot print as it is (a
non-ASCII letter, say) shows `CHECK` instead, and the message says to copy the name from
`lakana check list`. Here are the rows that the failed check of the run above found:

<!-- source: docs/examples/check-your-data/run.sh#check-rows -->
```bash
lakana check rows "$RUN_ID" silver.station_temperatures/no_missing_station --env dev
```

<!-- output: check-your-data#check-rows -->
```text
┏━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ station_id ┃ avg_temp_c ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│            │ 2.0        │
└────────────┴────────────┘
1 rows
```

The check found one average without a station. The table was never published, so no query of it
works, yet the rows are shown: Lakana kept the data of the failed run for a while (see
[How long the rows can be shown](#how-long-the-rows-can-be-shown)). The command takes the output
options of `lakana sql`: `--out FILE` writes every row to a file, `--max-rows N` shows more or
fewer rows, and `--timeout SECONDS` limits the wait.

### What it shows

- **The rows the check found in that run, as they were then.** This is also so after the table was
  set back: the check's own `SELECT` against the table finds nothing then, and `lakana check rows`
  still shows what the check saw.
- **Every other table the check reads, as it is now.** A check that joins the checked table with
  a table of stations shows the stations as they are today, not as they were in that run.
- **Only the columns the table still has.** A column is shown when the table has it today under
  the same name and with the same type, and it is still that column, not one that was dropped
  and added again under the name. The messages below call this "in the same form". A column that
  the failed run added and that was set back with the table, or that was dropped or changed
  since, is not shown, because the rules that hide data from you (row filters and column masks) are written for the
  table as it is now and cannot be applied to a column that it no longer has. A check whose
  `SELECT` names such a column cannot list its rows.
- **What you may read.** The command is a query of yours: it runs the check's `SELECT` under your
  own rights, row filters and column masks. Another person with other rules sees other rows or
  values.

### What you need

You need the right to read the run (`READ_ENVIRONMENT` on the environment, or `RUN`) and to submit
queries (`SUBMIT_QUERY`; the roles `ANALYST` and above have it), and the right to read every table
the check's SQL reads (`READ_TABLE`). The check's SQL becomes the text of your query, so without
that right the command refuses before anything else: ``You may not read every table this check
reads, so its rows and its SQL are not shown to you. Ask an administrator for READ on the tables
the check reads, or run the check's file from the project repository yourself.`` The message names
none of the tables. The SQL of such a check is also left out of `lakana project-version show`, which
counts the tables you may not read instead, as it does for an asset.

A task of a pipeline cannot use the command. A blocking check keeps the rows that failed away from
everything downstream, and a task that could read them would get them after all. It is a command
for a person, run outside the pipeline. A task that tries gets: ``A task cannot read the rows a
check found: they are shown to a person's own query, never to a pipeline. Run `lakana check rows`
yourself, outside the task.``

### How long the rows can be shown

Every write into a table leaves a **snapshot**: the state of the table that the run wrote. The
rows a check found are read from the snapshot of its run, so they can be shown as long as the
table keeps that snapshot. A **tag** is a name put on a snapshot, which keeps it although the
table has moved on.

- **A failed `block` check: seven days.** The run keeps the failed state of the table under a tag
  named `lakana-check-failed-` and the run's identifier. `lakana table maintain` removes the tag
  and the snapshot of a failed check once the snapshot is more than seven days old; exactly seven
  days old stays. The seven days run from the moment of the write, which is a little earlier than
  the failure, because the checks took their time. The removal needs a maintenance run: a table
  that nobody maintains keeps its failed snapshot, and so the rows, for longer. A snapshot that a
  run still in progress needs to set its table back stays when its tag goes, until that run has
  ended. A tag that someone creates by hand with the same prefix is removed after seven days as
  well.
- **A `keep` or `warn` check: as long as the table's history keeps the snapshot.** The table holds
  the data or has it in a snapshot that it keeps like any other; ordinary maintenance
  (`lakana table maintain`, by default snapshots older than seven days, but at least the last five)
  decides when that snapshot goes.

What maintenance did is in its report: `lakana table maintain` prints `removed 1 tag of a failed
check older than 7 days` after the expired snapshots, and the report of `lakana table maintain` as
JSON lists the names in `tags_removed`. The number of rows a check found stays in
`lakana check list` for good.

### When the command cannot show rows

The command then ends with a message that names the cause and what to do. `RUN`, `TABLE`, `CHECK`
and `ENV` stand for your own values in the messages below.

| You see | Why | What to do |
|---|---|---|
| ``The rows of this check can no longer be read: the snapshot it checked is no longer in `TABLE`. Maintenance removes a failed snapshot after 7 days, and a table that is dropped and built again starts anew. The number of rows the check found remains: `lakana check list --run RUN --env ENV`.`` | The seven days have passed and maintenance removed the snapshot, or the table was dropped, renamed or built again. | Nothing brings the rows back. The count is in `lakana check list`; to find the rows again, run the check's `SELECT` against the table as it is now. |
| ``This check has no rows to show from run RUN: `TABLE` is a view, so no snapshot was checked. Run the check's SQL yourself: `PATH` in the project.`` | The checked table is a view, and a view has no snapshots. | Run the SQL in `PATH` with `lakana sql`. |
| ``This check has no rows to show from run RUN: its SQL does not read `TABLE`, so no snapshot of it was checked. Run the check's SQL yourself: `PATH` in the project.`` | The check's SQL does not read the table it belongs to. | Run the SQL in `PATH` with `lakana sql`. |
| ``This check has no rows to show from run RUN: the check could not run. Its error is in `lakana check list --run RUN --env ENV`.`` | The check ended with an error and counted no rows, so no snapshot was checked. | Read the error with the command, and fix the check or its table. |
| ``This check has no rows to show from run RUN: no snapshot of the table was checked.`` | The result names no snapshot for a reason that is not one of the three above (a table that had no snapshot, for example). | Run the check's SQL yourself with `lakana sql`. |
| ``Run RUN has no result for the check `CHECK`. Its checks: `lakana check list --run RUN --env ENV`.`` | The run has no check of that name: a typo, or another run. | List the run's checks and copy the name from there. |
| ``The project version of run RUN is no longer stored, so the check's SQL is not known. The number of rows remains: `lakana check list --run RUN --env ENV`.`` | The project version that the run used was removed, and with it the check's SQL. | The count is in `lakana check list`. Run the check's file from your repository by hand. |
| ``The SQL of this check stored with run RUN cannot be read, so its rows cannot be shown. Deploy the project again (`lakana deploy --env ENV`) and run it; the number of rows remains: `lakana check list --run RUN --env ENV`.`` | The check's SQL stored with the run cannot be read; a deploy only stores SQL it can read, so it was changed afterwards. | Deploy the project again and run it. |
| ``The check result this query reads is no longer there: it was recorded again by a new attempt of its task, or removed with its run. Run `lakana check rows` again.`` | While the query waited, the task of the check was started again and recorded its results anew, or the run was removed. | Run the same `lakana check rows` command again. It shows the rows of the new result, or says that the run has no such check any more. |
| ``The rows of this check cannot be read from `TABLE`: the check result is not one of this table as it exists now, or was removed with its run.`` | The table was dropped and built again under its name, so the result belongs to a table that no longer exists; or the result was removed with its run. | Nothing brings the rows back. If the run still exists, the count is in `lakana check list --run RUN --env ENV`; run the check's `SELECT` against the table as it is now. |
| ``The column `COLUMN` of the checked snapshot is not shown: `TABLE` does not have it in this form any more (it was added by the run that failed, or dropped or changed since), so the rows this check found cannot be listed with it. The number of rows the check found remains: `lakana check list --run RUN --env ENV`.`` | The check's `SELECT` names a column that is not shown (see above), in any letter case or through a table alias (`o.note`). | Nothing brings the column back for this check. The count is in `lakana check list`; the `SELECT` against the table as it is now shows what is there today. |
| ``None of the columns of the checked snapshot is in `TABLE` in the same form any more, so the rows this check found cannot be listed. The number of rows the check found remains: `lakana check list --run RUN --env ENV`.`` | The table was built again with other columns. | The count is in `lakana check list`. |
| ``A row filter or column mask of yours on `TABLE` uses the column `COLUMN`, which is not the same column in the checked snapshot and in the table now (it was added, dropped or renamed since, or another column took its name), so the rows this check found cannot be shown under it. The number of rows the check found remains: `lakana check list --run RUN --env ENV`.`` | A row filter or column mask of yours reads a column that was added, dropped or changed since. Lakana never drops a filter to make a read work. | Ask the person who manages your policies to adjust it. The count is in `lakana check list`. |

The messages that open with `This check has no rows to show`, the one about a run without that
check and the one about the project version come back at once, as the command's error (the SDK
raises `lakana.errors.Conflict` for the first and the last, `lakana.errors.NotFound` for the
second). The others are the failure of the query, after it was queued.

### Join the failing rows with other data

`lakana check rows` runs the check's `SELECT` exactly as the check's file holds it, so it cannot
join the failing rows with another table. To do that, run the check's SQL in a query of your own
and add the join: copy the `SELECT` from the check's file (`checks/silver/station_temperatures/` in
the project) into `lakana sql`. Such a query reads the table as it is now. That finds the rows only
while they are still in the table: after a `keep` check, a `warn` check or a check of a table your
project only reads. It does not find them after a set-back, when the table holds its earlier data
again, nor after a first build that failed a check: that table is not published, and no query of
your own can read it. `lakana check rows` is the way to see those rows.

To find where a row comes from, look at the data the table was built from. Here, the average without
a station comes from a reading without a station:

<!-- source: docs/examples/check-your-data/run.sh#find-rows -->
```bash
lakana sql "SELECT station_id, temp_c FROM bronze.readings WHERE station_id IS NULL"
```

<!-- output: check-your-data#find-rows -->
```text
┏━━━━━━━━━━━━┳━━━━━━━━┓
┃ station_id ┃ temp_c ┃
┡━━━━━━━━━━━━╇━━━━━━━━┩
│            │ 2.0    │
└────────────┴────────┘
1 rows
```

## Fix it and run again

Fix the cause. Here, the reading without a station gets one; then commit, deploy and run again:

<!-- source: docs/examples/check-your-data/run.sh#fix-deploy -->
```bash
git add -A
git commit -m "Give every reading a station"
lakana deploy --env dev
```

<!-- output -->
```text
Queued deploy bf2db33d-d673-4643-b218-0b9db857f442
Deployed readings in dev at 63c594f30bb5da48a5736724ffe731bcc85fdde6
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ change      ┃ table           ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ sql_changed │ bronze.readings │
└─────────────┴─────────────────┘
Checks:
  silver.station_temperatures: has_rows, no_missing_station, plausible_range (warn)
Hint: run 'lakana run --env dev' to run what you just deployed.
```

<!-- source: docs/examples/check-your-data/run.sh#run-fixed -->
```bash
lakana run --env dev
```

<!-- output -->
```text
Run 2b8a18cd-b434-41c5-8d77-2d075cde3b9d succeeded
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ asset                       ┃ status    ┃ attempt ┃ rows ┃ error                                            ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ bronze.readings             │ succeeded │ 1/3     │ 6    │                                                  │
│ silver.station_temperatures │ succeeded │ 1/3     │ 3    │ note: 1 check warned: `plausible_range` (1 row). │
└─────────────────────────────┴───────────┴─────────┴──────┴──────────────────────────────────────────────────┘
Not shown: duration. Use --output json to see it.

silver.station_temperatures   warned   3 of 3 checks passed
  has_rows                    passed
  no_missing_station          passed
  plausible_range             warned   1 row
```

The run succeeded. `no_missing_station` passes, and the table is published. `plausible_range` still
warns, as the next section explains, and the run says so in the `error` column of the table,
as a `note`.

When the cause was in the code of your project, you deploy before you run again, as above. If the
cause was in data that your project only reads, and the project did not change, there is no new
deploy. `lakana runs resume RUN_ID --env dev` then builds again the tasks that failed, and the
ones after them, in a new run. It is refused after a deploy, because the project is no longer the
one that run used: run the new project with `lakana run` instead.

### When the table already had data

The first run built the table, so it was left not published. When a table already holds data and a
later run fails a check, the table is set back. Change `assets/bronze/readings.sql` back to the
first version of this guide (the reading `'C3', 2.0` becomes `NULL, 2.0` again), then commit,
deploy and run:

<!-- source: docs/examples/check-your-data/run.sh#break-deploy -->
```bash
git add -A
git commit -m "Break the data again"
lakana deploy --env dev
```

<!-- output -->
```text
[master 9445d5e] Break the data again
 1 file changed, 1 insertion(+), 1 deletion(-)
Queued deploy 75e37143-ae8a-497c-8c27-0e21125220b4
Deployed readings in dev at 9445d5e1939386473af3aa0c7d19ccc6130e891b
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ change      ┃ table           ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ sql_changed │ bronze.readings │
└─────────────┴─────────────────┘
Checks:
  silver.station_temperatures: has_rows, no_missing_station, plausible_range (warn)
Hint: run 'lakana run --env dev' to run what you just deployed.
```

<!-- source: docs/examples/check-your-data/run.sh#run-rolled-back -->
```bash
lakana run --env dev
```

<!-- output -->
```text
Run 18a4496e-4cda-4786-9589-0086621190e0 failed
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ asset                       ┃ status    ┃ attempt ┃ error                                                            ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ bronze.readings             │ succeeded │ 1/3     │                                                                  │
│ silver.station_temperatures │ failed    │ 1/3     │ Check `silver.station_temperatures/no_missing_station` failed: 1 │
│                             │           │         │ row (checks/silver/station_temperatures/no_missing_station.sql). │
│                             │           │         │ The table was set back to what it was before this run, and       │
│                             │           │         │ nothing downstream was built. Look at the rows: `lakana check    │
│                             │           │         │ rows 18a4496e-4cda-4786-9589-0086621190e0                        │
│                             │           │         │ silver.station_temperatures/no_missing_station --env dev`. After │
│                             │           │         │ a fix, deploy and run again; if the project did not change:      │
│                             │           │         │ `lakana runs resume 18a4496e-4cda-4786-9589-0086621190e0 --env   │
│                             │           │         │ dev`.                                                            │
└─────────────────────────────┴───────────┴─────────┴──────────────────────────────────────────────────────────────────┘
Not shown: duration, rows. Use --output json to see them.

silver.station_temperatures   FAILED   2 of 3 checks passed, rolled back
  has_rows                    passed
  no_missing_station          FAILED   1 row
  plausible_range             warned   1 row
```

The message reads `The table was set back to what it was before this run`, and the table holds the
rows of the run that succeeded, not the failing ones:

<!-- source: docs/examples/check-your-data/run.sh#after-rollback -->
```bash
lakana sql "SELECT station_id, avg_temp_c FROM silver.station_temperatures ORDER BY station_id"
```

<!-- output: check-your-data#after-rollback -->
```text
┏━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ station_id ┃ avg_temp_c ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ A1         │ 5.0        │
│ B2         │ 2.0        │
│ C3         │ 500.5      │
└────────────┴────────────┘
3 rows
```

The rows that the check found are not in the table any more, and `lakana check rows` still shows
them, as they were in that run. `$RUN3` stands for the identifier of this run, as `$RUN_ID` did
above:

<!-- source: docs/examples/check-your-data/run.sh#check-rows-rolled-back -->
```bash
lakana check rows "$RUN3" silver.station_temperatures/no_missing_station --env dev
```

<!-- output: check-your-data#check-rows-rolled-back -->
```text
┏━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ station_id ┃ avg_temp_c ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│            │ 2.0        │
└────────────┴────────────┘
1 rows
```

## Five checks to copy

Each file below is complete. Put it at the path shown and deploy. The five checks use one table,
`silver.station_temperatures`, with a station's name in it, and a table of stations,
`bronze.stations`. If you follow this guide, **replace** the silver asset of the guide with the first
file below and **add** the second; the checks then work as they are. With your own tables, change
the table and column names.

`assets/silver/station_temperatures.sql`, now with the name of each station:

<!-- source: docs/examples/check-your-data/patterns/assets/silver/station_temperatures.sql -->
```sql
/* lakana
description: The average temperature of each station, with the station's name.
columns:
  station_id: The station that took the readings.
  station_name: The station's name, empty for a station that is not in bronze.stations.
  avg_temp_c: The average temperature in degrees Celsius.
*/
SELECT r.station_id, s.name AS station_name, AVG(r.temp_c) AS avg_temp_c
FROM bronze.readings AS r
LEFT JOIN bronze.stations AS s ON s.station_id = r.station_id
GROUP BY r.station_id, s.name
```

`assets/bronze/stations.sql`, the stations that Lakana knows:

<!-- source: docs/examples/check-your-data/patterns/assets/bronze/stations.sql -->
```sql
/* lakana
description: The weather stations Lakana knows, written by hand.
columns:
  station_id: The station's id.
  name: The station's name.
*/
SELECT 'A1' AS station_id, 'Harbour' AS name
UNION ALL SELECT 'B2', 'Hilltop'
UNION ALL SELECT 'C3', 'Airfield'
```

Because the silver asset reads `bronze.stations`, Lakana builds the stations first. The checks:

**No missing value.** `checks/silver/station_temperatures/no_missing_value.sql`:

<!-- source: docs/examples/check-your-data/patterns/checks/silver/station_temperatures/no_missing_value.sql -->
```sql
/* lakana
description: Every station has an average temperature.
*/
SELECT station_id
FROM silver.station_temperatures
WHERE avg_temp_c IS NULL
```

**A unique key.** `checks/silver/station_temperatures/unique_key.sql` returns each key that
appears more than once:

<!-- source: docs/examples/check-your-data/patterns/checks/silver/station_temperatures/unique_key.sql -->
```sql
/* lakana
description: Each station appears once.
*/
SELECT station_id, COUNT(*) AS copies
FROM silver.station_temperatures
GROUP BY station_id
HAVING COUNT(*) > 1
```

**Accepted values.** `checks/silver/station_temperatures/accepted_values.sql` returns a value that
is not on the list. A missing value is not on the list either, but `NOT IN` does not return it;
add `no_missing_value` for that:

<!-- source: docs/examples/check-your-data/patterns/checks/silver/station_temperatures/accepted_values.sql -->
```sql
/* lakana
description: Only the three known stations report.
*/
SELECT station_id
FROM silver.station_temperatures
WHERE station_id NOT IN ('A1', 'B2', 'C3')
```

**A key that exists in another table.**
`checks/silver/station_temperatures/station_is_known.sql` returns each row whose key is missing
from the other table:

<!-- source: docs/examples/check-your-data/patterns/checks/silver/station_temperatures/station_is_known.sql -->
```sql
/* lakana
description: Every station is in the table of stations.
*/
SELECT t.station_id
FROM silver.station_temperatures AS t
LEFT JOIN bronze.stations AS s ON s.station_id = t.station_id
WHERE s.station_id IS NULL
```

A check reads the other table as it is at that moment. Lakana does not build `bronze.stations`
before the check unless the checked table's asset reads it too. In this project
`silver.station_temperatures` joins `bronze.stations`, so it is built first. Put a check that
compares two tables on the one that is built later.

**A row count that is not zero.** `checks/silver/station_temperatures/not_empty.sql` returns one
row, a message, when the table has no rows:

<!-- source: docs/examples/check-your-data/patterns/checks/silver/station_temperatures/not_empty.sql -->
```sql
/* lakana
description: The table is not empty.
*/
SELECT 'silver.station_temperatures has no rows' AS problem
WHERE NOT EXISTS (SELECT 1 FROM silver.station_temperatures)
```

Put the five files next to the three of the guide, change the readings back to the fixed version
(every reading has a station), then commit, deploy and run:

<!-- source: docs/examples/check-your-data/run.sh#patterns-deploy -->
```bash
git add -A
git commit -m "Five more checks"
lakana deploy --env dev
```

<!-- output -->
```text
[master 4cbf9ca] Five more checks
 8 files changed, 47 insertions(+), 5 deletions(-)
 create mode 100644 assets/bronze/stations.sql
 create mode 100644 checks/silver/station_temperatures/accepted_values.sql
 create mode 100644 checks/silver/station_temperatures/no_missing_value.sql
 create mode 100644 checks/silver/station_temperatures/not_empty.sql
 create mode 100644 checks/silver/station_temperatures/station_is_known.sql
 create mode 100644 checks/silver/station_temperatures/unique_key.sql
Queued deploy 63b09447-3cbb-49f9-9d21-344d5d8905c5
Deployed readings in dev at 4cbf9cad29a0665bf2e763c07dee7e789a2d5964
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ change           ┃ table                       ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ added            │ bronze.stations             │
│ sql_changed      │ bronze.readings             │
│ sql_changed      │ silver.station_temperatures │
│ metadata_changed │ silver.station_temperatures │
└──────────────────┴─────────────────────────────┘
Checks:
  silver.station_temperatures: accepted_values, has_rows, no_missing_station, no_missing_value, not_empty, plausible_range (warn), station_is_known, unique_key
Hint: run 'lakana run --env dev' to run what you just deployed.
```

<!-- source: docs/examples/check-your-data/run.sh#run-patterns -->
```bash
lakana run --env dev
```

The table has eight checks now, and all of them pass or warn:

<!-- output -->
```text
Run 41df4888-7a57-42c0-b124-233b64856963 succeeded
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ asset                       ┃ status    ┃ attempt ┃ rows ┃ error                                            ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ bronze.readings             │ succeeded │ 1/3     │ 6    │                                                  │
│ bronze.stations             │ succeeded │ 1/3     │ 3    │                                                  │
│ silver.station_temperatures │ succeeded │ 1/3     │ 3    │ note: 1 check warned: `plausible_range` (1 row). │
└─────────────────────────────┴───────────┴─────────┴──────┴──────────────────────────────────────────────────┘
Not shown: duration. Use --output json to see it.

silver.station_temperatures   warned   8 of 8 checks passed
  accepted_values             passed
  has_rows                    passed
  no_missing_station          passed
  no_missing_value            passed
  not_empty                   passed
  plausible_range             warned   1 row
  station_is_known            passed
  unique_key                  passed
```

## Checks from the asset's header

The unique key and the key that exists in another table need no file of their own. Declare
`key: [station_id]` in the header of `assets/silver/station_temperatures.sql`, and each deploy adds
the checks `key_unique` and `key_not_null`; declare `references:` and it adds a `ref_<column>`
check for each entry, and builds a referenced table of the project before the table that
references it. `lakana check list` shows them as `key_unique (generated)`, a result's path is the
asset's file and its header's line (`assets/silver/station_temperatures.sql:1`), and
`lakana check rows` shows their rows like those of any check. They block unless `checks:` in the
header or the project's default says otherwise. Their names, the run order, the texts of a check
whose columns or types do not fit and what to do about each are in
[Generated checks](../reference/checks.md#generated-checks); the header's fields are in
[Model fields](../reference/asset-front-matter.md#model-fields-kind-grain-key-and-references).
[Declare a table's model](declare-a-tables-model.md) builds a star whose generated checks warn,
fail and pass.

## Block, keep or warn

`on_failure` says what a check does when it finds rows. It has three values:

| `on_failure` | The table | What is built from it | Choose it when |
|---|---|---|---|
| `block` | Set back to what it was before the run; a first build is left not published. | Not built. | Bad data must not stay where readers or other tables find it. The usual choice, and the value of a check unless `lakana.yaml` says otherwise. |
| `keep` | Keeps the data this run wrote, and readers can look at it. It is marked `kept`. | Not built, in this run and in every later run, until the table's checks pass. | You want to look at the failing rows in the table itself, and nothing may be built from it before you have decided. |
| `warn` | Stays as the run wrote it. | Built as always. The run succeeds, with a note. | You are trying a check out, or the fault is worth knowing about and not worth stopping for. |

The value in force for a check is its own `on_failure`, else the default in `lakana.yaml`, else
`block`. The comment of a check takes two keys, `description` and `on_failure`. Any other key, and
any other value of `on_failure`, is refused at deploy and the message lists the right ones.

`keep` can be used on a table your project builds with `write: replace` (the default, SQL or
Python) and on a table it only reads, where it stops what reads the table in that run
and marks nothing (see [A table your project only reads](#a-table-your-project-only-reads)). A
deploy refuses it on an asset that appends (`write: append`, or `materialize: incremental`), on
one that merges (`write: merge`) and on a view. A kept table clears when it is built again and
passes its checks; building an appending or merging asset again only adds or updates rows, so the
rows that failed would stay, and a view holds no data to keep. Use `block` there: it takes a
failed increment or merge out again, and for a view it stops what reads it. The messages are in
[Checks](../reference/checks.md#what-a-deploy-refuses).

### Block

This is what the guide has shown so far: the table is set back and nothing downstream is built.
[What a failed check does](#what-a-failed-check-does) lists the messages.

### Warn

A check that should only report says so in its comment:

<!-- source: docs/examples/check-your-data/project/checks/silver/station_temperatures/plausible_range.sql -->
```sql
/* lakana
description: An average below -90 or above 60 degrees is a sensor fault.
on_failure: warn
*/
SELECT station_id, avg_temp_c
FROM silver.station_temperatures
WHERE avg_temp_c < -90 OR avg_temp_c > 60
```

A check that warns leaves the table as the run wrote it. The run succeeds, the check's line reads
`warned` with the number of rows, and the task carries a note. The rows are in the published
table, so you find them with the check's own `SELECT`:

<!-- source: docs/examples/check-your-data/run.sh#warn-rows -->
```bash
lakana sql "SELECT station_id, avg_temp_c FROM silver.station_temperatures WHERE avg_temp_c < -90 OR avg_temp_c > 60"
```

<!-- output: check-your-data#warn-rows -->
```text
┏━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ station_id ┃ avg_temp_c ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ C3         │ 500.5      │
└────────────┴────────────┘
1 rows
```

A warning check that cannot run is reported too, and does not stop anything.

### Keep

A check with `on_failure: keep` fails its task and leaves the table as the run wrote it, with the
failing data in it. Nothing that reads the table is built until its checks pass. Use it where you
want to look at the failing rows in the table, with your own queries, before you decide what to do.

The guide adds two assets. The first writes the battery level of three sensors, and one of them is
140 percent:

<!-- source: docs/examples/check-your-data/keep/assets/bronze/sensor_log.sql -->
```sql
/* lakana
description: The battery level of three sensors, written by hand.
columns:
  sensor_id: The sensor.
  battery_pct: The battery level in percent.
*/
SELECT 'S1' AS sensor_id, 80 AS battery_pct
UNION ALL SELECT 'S2', 35
UNION ALL SELECT 'S3', 140
```

The second lists the sensors whose battery is low, from the first table:

<!-- source: docs/examples/check-your-data/keep/assets/silver/battery_report.sql -->
```sql
/* lakana
description: The sensors whose battery is below 40 percent.
columns:
  sensor_id: The sensor.
  battery_pct: The battery level in percent.
*/
SELECT sensor_id, battery_pct
FROM bronze.sensor_log
WHERE battery_pct < 40
```

The check of the first table keeps:

<!-- source: docs/examples/check-your-data/keep/checks/bronze/sensor_log/battery_in_range.sql -->
```sql
/* lakana
description: A battery level is between 0 and 100 percent.
on_failure: keep
*/
SELECT sensor_id, battery_pct
FROM bronze.sensor_log
WHERE battery_pct < 0 OR battery_pct > 100
```

Commit and deploy. The list of checks marks it `(keep)`:

<!-- source: docs/examples/check-your-data/run.sh#keep-deploy -->
```bash
git add -A
git commit -m "Keep the battery levels when they fail"
lakana deploy --env dev
```

<!-- output -->
```text
[master 79a8960] Keep the battery levels when they fail
 3 files changed, 25 insertions(+)
 create mode 100644 assets/bronze/sensor_log.sql
 create mode 100644 assets/silver/battery_report.sql
 create mode 100644 checks/bronze/sensor_log/battery_in_range.sql
Queued deploy 8b0a234f-0256-40a1-a4b9-b993cfa3c78b
Deployed readings in dev at 79a89607944f100213ad3fa9575f9cc0d402b8e0
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┓
┃ change ┃ table                 ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━┩
│ added  │ bronze.sensor_log     │
│ added  │ silver.battery_report │
└────────┴───────────────────────┘
Checks:
  bronze.sensor_log: battery_in_range (keep)
  silver.station_temperatures: accepted_values, has_rows, no_missing_station, no_missing_value, not_empty, plausible_range (warn), station_is_known, unique_key
Hint: run 'lakana run --env dev' to run what you just deployed.
```

Run the new table and what reads it:

<!-- source: docs/examples/check-your-data/run.sh#keep-run -->
```bash
lakana run --select bronze.sensor_log --downstream --env dev
```

<!-- output -->
```text
Run 8b0403e4-d4a8-41f0-a686-5580aa0d048d failed
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ asset                 ┃ status          ┃ attempt ┃ error                                                            ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ bronze.sensor_log     │ failed          │ 1/3     │ Check `bronze.sensor_log/battery_in_range` failed: 1 row         │
│                       │                 │         │ (checks/bronze/sensor_log/battery_in_range.sql). The table keeps │
│                       │                 │         │ this run's data (`on_failure: keep`), and nothing that reads it  │
│                       │                 │         │ was built. Repair the data or the asset; after a fix, deploy and │
│                       │                 │         │ run again; if the project did not change: `lakana runs resume    │
│                       │                 │         │ 8b0403e4-d4a8-41f0-a686-5580aa0d048d --env dev`.                 │
│ silver.battery_report │ upstream_failed │         │                                                                  │
└───────────────────────┴─────────────────┴─────────┴──────────────────────────────────────────────────────────────────┘
Not shown: duration, rows. Use --output json to see them.

bronze.sensor_log   FAILED         0 of 1 checks passed
  battery_in_range  failed (kept)  1 row
```

Read it as you read a blocking failure, with these differences:

- The message says `The table keeps this run's data (`on_failure: keep`), and nothing that reads
  it was built.` Nothing was set back, and the check reads `failed (kept)`.
- `silver.battery_report` is `upstream_failed`, as after any failed table.
- The table now has the state `kept`, and says why (the `metadata_location` row is left out below,
  as above; the table of columns that follows it is what `table get` always prints):

<!-- source: docs/examples/check-your-data/run.sh#keep-table-get -->
```bash
lakana table get bronze.sensor_log --env dev
```

<!-- output -->
```text
┌───────────────────┬──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ name              │ sensor_log                                                                                       │
│ namespace         │ bronze                                                                                           │
│ kind              │ TABLE                                                                                            │
│ state             │ kept                                                                                             │
│ uuid              │ 02266596-b764-47de-831e-21eb057d59d4                                                             │
└───────────────────┴──────────────────────────────────────────────────────────────────────────────────────────────────┘
Kept after a failed check (`bronze.sensor_log/battery_in_range`, run 8b0403e4-d4a8-41f0-a686-5580aa0d048d): the table 
holds the data that failed. Tasks do not read it until its checks pass.
┏━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┓
┃ name        ┃ type   ┃ nullable ┃
┡━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━┩
│ sensor_id   │ string │ True     │
│ battery_pct │ int    │ True     │
└─────────────┴────────┴──────────┘
```

- **Anyone can still read the table**, with the failing row in it. `kept` stops tasks, not people:

<!-- source: docs/examples/check-your-data/run.sh#keep-rows -->
```bash
lakana sql "SELECT sensor_id, battery_pct FROM bronze.sensor_log WHERE battery_pct > 100"
```

<!-- output: check-your-data#keep-rows -->
```text
┏━━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ sensor_id ┃ battery_pct ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ S3        │ 140         │
└───────────┴─────────────┘
1 rows
```

  After the data is repaired the table no longer holds that row. `lakana check rows` for the
  check `bronze.sensor_log/battery_in_range` and the run that kept the table still shows it, as
  it was then (see [Look at the rows that failed](#look-at-the-rows-that-failed)).

#### What a kept table does to the next runs

A kept table stays kept until its checks pass, so a later run cannot read it by accident. Run only
the table that reads it:

<!-- source: docs/examples/check-your-data/run.sh#keep-run-reader -->
```bash
lakana run --select silver.battery_report --env dev
```

<!-- output -->
```text
Run 3f06fc30-1a1c-4ab7-8310-0d6575672bf6 failed
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ asset                 ┃ status          ┃ attempt ┃ error                                                            ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ bronze.sensor_log     │ failed          │ 1/3     │ Check `bronze.sensor_log/battery_in_range` failed: 1 row         │
│                       │                 │         │ (checks/bronze/sensor_log/battery_in_range.sql). Look at the     │
│                       │                 │         │ rows: `lakana check rows 3f06fc30-1a1c-4ab7-8310-0d6575672bf6    │
│                       │                 │         │ bronze.sensor_log/battery_in_range --env dev`.                   │
│                       │                 │         │ `bronze.sensor_log` was kept after a failed check in run         │
│                       │                 │         │ 8b0403e4-d4a8-41f0-a686-5580aa0d048d and still fails, so the     │
│                       │                 │         │ assets that read it were not built. Build it again (`lakana run  │
│                       │                 │         │ --select bronze.sensor_log --env dev`), or repair the data and   │
│                       │                 │         │ run again.                                                       │
│ silver.battery_report │ upstream_failed │         │                                                                  │
└───────────────────────┴─────────────────┴─────────┴──────────────────────────────────────────────────────────────────┘
Not shown: duration, rows. Use --output json to see them.

bronze.sensor_log   FAILED         0 of 1 checks passed, kept after a failed check
  battery_in_range  failed (kept)  1 row
```

The run did not build `silver.battery_report`. Because it reads a table that was kept, Lakana ran
that table's checks first. The checks fail again, so the task of the table fails with a message that
ends with
`` `bronze.sensor_log` was kept after a failed check in run RUN and still fails, so the assets that
read it were not built. Build it again (`lakana run --select bronze.sensor_log --env dev`), or
repair the data and run again.`` The table is not changed and stays kept. The heading of the group
says `kept after a failed check` where a table that your project only reads says `not built by
this project`.

Four more things follow from "nothing that reads it is built":

- **A task that reads a kept table without a check task before it is stopped.** This happens to a
  run that started before the table was kept, and to every task of **another project**. The task
  fails on its first attempt with this message, which names the project that can clear it:
  `` `silver.orders` was kept after a failed check (`silver.orders/has_a_hundred`, run RUN) and has not passed its checks since, so this task does not read it. A run of the project that builds `silver.orders` clears this: build it again there (`lakana run --select silver.orders --env dev`), or repair the data and run that project. If no project builds it any more, drop it: `lakana table delete silver.orders --env dev`.``
  When the mark names a check that could not run, the first sentence reads `` `silver.orders` was kept after a check that could not run (`silver.orders/has_a_hundred`, run RUN) and has not passed its checks since, so this task does not read it.`` (the REST API answers 409).
  Another project cannot clear the mark, also when it has a check of its own on the table and the
  check passes, because its check may be weaker than the one that failed. It waits for the project
  that builds the table.
- **A deploy that removes the asset of a kept table warns you.** No project builds the table
  then, so no run can clear the mark. The deploy still succeeds, and `lakana deploy` prints
  `` warning: this deploy removes the asset of `silver.orders`, which is kept after its
  checks failed or could not run, or set back to data whose checks are not known; add it back and
  build it, or drop the table`` on stderr (the JSON output has it in
  `warnings`). Add the asset back and build it, or drop the table with `lakana table delete`. You
  see the warning for the tables you may read.
- **A check that reads a kept table of another project cannot run.** A check whose SQL reads a
  table that another project builds and that is kept (a join, for example) is recorded as
  `errored`, with the error `a table this check reads
  was kept after its checks failed or could not run, or was set back to data whose checks are not
  known, and has not passed its checks since, so the check could not read it. `lakana table get` on each table the check reads shows which one, and why.`, and acts by its
  own `on_failure`. A check may read a kept table of its own project: counting rows builds
  nothing. When every `keep` check of a task could not run for that reason, the task's own
  table is not marked, unless it carried a mark already: it was built again, no check could run on
  the new data, so the mark moves to the new run and names the check as one that could not run.
  `lakana table get` then says ``Kept after a check that could not run (`CHECK`, run RUN): the table holds data no check could run on. Tasks do not read it until its checks pass.`` Readers stay refused until
  the table passes its checks. The task's message says: `` The checks of `TABLE` could not run, so nothing that reads it was built. After the kept table that a check reads is cleared, run again; if the project did not change: `lakana runs resume RUN --env ENV`.``
  Run `lakana table get` on the tables the check reads to see which one is kept.
- **A project that removes the asset of a kept table** has no run that clears the mark. Add the
  asset back and build it, or drop the table with `lakana table delete TABLE --env dev`, as the
  message of a stopped task says. Reads outside tasks work throughout.

#### How a kept table clears

Repair the cause, then let the project run:

- Fix the data or the asset, commit, deploy (when the project changed) and run. The checks of the
  table pass, the mark is cleared, and the table is `published` again. Here the battery level is
  capped at 100:

<!-- source: docs/examples/check-your-data/run.sh#keep-repair-deploy -->
```bash
git add -A
git commit -m "Cap the battery level at 100"
lakana deploy --env dev
```

<!-- output -->
```text
[master 9bb452d] Cap the battery level at 100
 1 file changed, 1 insertion(+), 1 deletion(-)
Queued deploy df9af374-9fef-445d-8e41-22cc002588f6
Deployed readings in dev at 9bb452d6dc61bd400a74f3b9c3975bed4efc3f35
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
┃ change      ┃ table             ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━┩
│ sql_changed │ bronze.sensor_log │
└─────────────┴───────────────────┘
Checks:
  bronze.sensor_log: battery_in_range (keep)
  silver.station_temperatures: accepted_values, has_rows, no_missing_station, no_missing_value, not_empty, plausible_range (warn), station_is_known, unique_key
Hint: run 'lakana run --env dev' to run what you just deployed.
```

<!-- source: docs/examples/check-your-data/run.sh#keep-run-repaired -->
```bash
lakana run --select bronze.sensor_log --downstream --env dev
```

<!-- output -->
```text
Run 62b6f24b-9ce4-4c5e-9a1e-1a040b081f3c succeeded
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━┳━━━━━━━┓
┃ asset                 ┃ status    ┃ attempt ┃ duration ┃ rows ┃ error ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━╇━━━━━━━┩
│ bronze.sensor_log     │ succeeded │ 1/3     │ 1.0s     │ 3    │       │
│ silver.battery_report │ succeeded │ 1/3     │ 0.0s     │ 1    │       │
└───────────────────────┴───────────┴─────────┴──────────┴──────┴───────┘

bronze.sensor_log   passed   1 of 1 checks passed
  battery_in_range  passed
```

<!-- source: docs/examples/check-your-data/run.sh#keep-published -->
```bash
lakana table get bronze.sensor_log --env dev
```

<!-- output -->
```text
┌───────────────────┬──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ name              │ sensor_log                                                                                       │
│ namespace         │ bronze                                                                                           │
│ kind              │ TABLE                                                                                            │
│ state             │ published                                                                                        │
│ uuid              │ 02266596-b764-47de-831e-21eb057d59d4                                                             │
└───────────────────┴──────────────────────────────────────────────────────────────────────────────────────────────────┘
┏━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━┓
┃ name        ┃ type   ┃ nullable ┃
┡━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━┩
│ sensor_id   │ string │ True     │
│ battery_pct │ int    │ True     │
└─────────────┴────────┴──────────┘
```

- If the project did not change, because the cause was in data that it only reads, use
  `lakana runs resume RUN_ID --env dev` with the id of the run that kept the table. It builds the
  kept table again in a new run, which writes it and checks it, and then the tables after it. It
  also does this for a run that was cancelled before the table's check ran.
- A run that rebuilds the table with a version of the project that has no checks for it clears the
  mark too, so removing a check you no longer want is a way out. The rebuild is needed: a run
  that builds only what reads the table stops at the table's check task, with the message
  `` `TABLE` was kept after a failed check in run RUN, and the project has no check for it any more, so nothing can pass. Only building it again clears this: `lakana run --select TABLE --env ENV`.``
- A later run in which a `block` check of the table fails goes back to the kept data, not to an
  older state, and the table stays kept.

The audit log records both steps, as `TABLE_KEPT` and `TABLE_KEPT_CLEARED`; see
[Checks](../reference/checks.md#what-is-audited).

## A table your project only reads

A check can also belong to a table that no asset of your project builds, such as a table that
another team or a loading job fills. The project reads it, so it can check it. Here somebody loaded
`bronze.sensors`, which has a sensor without a station:

<!-- source: docs/examples/check-your-data/run.sh#source-table -->
```bash
lakana sql "SELECT 'S1' AS sensor_id, 'A1' AS station_id UNION ALL SELECT 'S2', NULL" \
  --into bronze.sensors --mode create
```

The project has an asset `silver.sensor_summary` that reads it, and a check of the table at
`checks/bronze/sensors/every_sensor_has_a_station.sql`:

<!-- source: docs/examples/check-your-data/source-check/checks/bronze/sensors/every_sensor_has_a_station.sql -->
```sql
/* lakana
description: Every sensor says which station it belongs to.
*/
SELECT sensor_id
FROM bronze.sensors
WHERE station_id IS NULL
```

A run gives such a table a task of its own, which runs the check before the assets that read the
table. If the check fails, those assets are not built. Nothing is set back, because this project
did not write the table:

<!-- source: docs/examples/check-your-data/run.sh#run-source -->
```bash
lakana run --env dev
```

<!-- output -->
```text
Run 2ca967e5-9c41-481d-bef6-47daa8a3da19 failed
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ asset                       ┃ status          ┃ attempt ┃ error                                                      ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ bronze.readings             │ succeeded       │ 1/3     │                                                            │
│ bronze.sensor_log           │ succeeded       │ 1/3     │                                                            │
│ bronze.sensors              │ failed          │ 1/3     │ Check `bronze.sensors/every_sensor_has_a_station` failed:  │
│                             │                 │         │ 1 row                                                      │
│                             │                 │         │ (checks/bronze/sensors/every_sensor_has_a_station.sql).    │
│                             │                 │         │ This project does not build `bronze.sensors`, so it was    │
│                             │                 │         │ not changed; the assets that read it were not built. After │
│                             │                 │         │ a fix, deploy and run again; if the project did not        │
│                             │                 │         │ change: `lakana runs resume                                │
│                             │                 │         │ 2ca967e5-9c41-481d-bef6-47daa8a3da19 --env dev`.           │
│ bronze.stations             │ succeeded       │ 1/3     │                                                            │
│ silver.battery_report       │ succeeded       │ 1/3     │                                                            │
│ silver.sensor_summary       │ upstream_failed │         │                                                            │
│ silver.station_temperatures │ succeeded       │ 1/3     │ note: 1 check warned: `plausible_range` (1 row).           │
└─────────────────────────────┴─────────────────┴─────────┴────────────────────────────────────────────────────────────┘
Not shown: duration, rows. Use --output json to see them.

bronze.sensor_log   passed   1 of 1 checks passed
  battery_in_range  passed
bronze.sensors               FAILED   0 of 1 checks passed, not built by this project
  every_sensor_has_a_station FAILED   1 row
silver.station_temperatures   warned   8 of 8 checks passed
  accepted_values             passed
  has_rows                    passed
  no_missing_station          passed
  no_missing_value            passed
  not_empty                   passed
  plausible_range             warned   1 row
  station_is_known            passed
  unique_key                  passed
```

`bronze.sensors` failed and `silver.sensor_summary` has the state `upstream_failed`. The rest of
the project, `silver.station_temperatures` here, was built as always. A check with `on_failure:
keep` on such a table does the same and does not mark it: the project did not write the table, so it
could not keep anything, and the table is checked again in every run. The table holds the data
it held, so you look at the rows with the check's `SELECT`. After the source is fixed, and if the
project did not change, `lakana runs resume RUN_ID --env dev` builds what did not run.

## A default for the whole project

The project has eight checks without an `on_failure` of their own, one that only warns and one
that keeps. To see the default work, add the block from [Adding checks to a pipeline that already
runs](#adding-checks-to-a-pipeline-that-already-runs) to `lakana.yaml`, then commit and deploy.
The deploy lists `(warn)` after each check that warns, and says how many took the value from
`lakana.yaml`; `(keep)` stays on the check that has it:

<!-- source: docs/examples/check-your-data/run.sh#default-deploy -->
```bash
git add -A
git commit -m "Warn by default"
lakana deploy --env dev
```

<!-- output -->
```text
[master a354b40] Warn by default
 1 file changed, 2 insertions(+)
Queued deploy 481f3a65-04a5-4c59-bed8-606ab98a5ac8
Deployed readings in dev at a354b409f8bde8e3dd0d87fa19d2212f2c4586cc
No changes.
Checks:
  bronze.sensor_log: battery_in_range (keep)
  bronze.sensors: every_sensor_has_a_station (warn)
  silver.station_temperatures: accepted_values (warn), has_rows (warn), no_missing_station (warn), no_missing_value (warn), not_empty (warn), plausible_range (warn), station_is_known (warn), unique_key (warn)
8 checks take `on_failure: warn` from lakana.yaml.
Hint: run 'lakana run --env dev' to run what you just deployed.
```

The same run that failed on the sensors now succeeds. The check of `bronze.sensors` warns, and the
asset that reads that table is built:

<!-- source: docs/examples/check-your-data/run.sh#default-run -->
```bash
lakana run --env dev
```

<!-- output -->
```text
Run e80eeb75-4b08-410c-ab78-0d7fc551093f succeeded
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ asset                       ┃ status    ┃ attempt ┃ error                                                       ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ bronze.readings             │ succeeded │ 1/3     │                                                             │
│ bronze.sensor_log           │ succeeded │ 1/3     │                                                             │
│ bronze.sensors              │ succeeded │ 1/3     │ note: 1 check warned: `every_sensor_has_a_station` (1 row). │
│ bronze.stations             │ succeeded │ 1/3     │                                                             │
│ silver.battery_report       │ succeeded │ 1/3     │                                                             │
│ silver.sensor_summary       │ succeeded │ 1/3     │                                                             │
│ silver.station_temperatures │ succeeded │ 1/3     │ note: 1 check warned: `plausible_range` (1 row).            │
└─────────────────────────────┴───────────┴─────────┴─────────────────────────────────────────────────────────────┘
Not shown: duration, rows. Use --output json to see them.

bronze.sensor_log   passed   1 of 1 checks passed
  battery_in_range  passed
bronze.sensors               warned   1 of 1 checks passed, not built by this project
  every_sensor_has_a_station warned   1 row
silver.station_temperatures   warned   8 of 8 checks passed
  accepted_values             passed
  has_rows                    passed
  no_missing_station          passed
  no_missing_value            passed
  not_empty                   passed
  plausible_range             warned   1 row
  station_is_known            passed
  unique_key                  passed
```

`lakana check list` shows the value in force for each check, whichever way it was set.

#### When a kept run is set back

A kept table can be built again and fail a `keep` check again: the mark moves to the new run. If
that run is then set back (you cancel it before its failure is recorded, or its checks run again
and a `block` check fails), the table holds the earlier data again. `lakana table get` shows what
that data was left with: its mark, naming the run of that data and not the one that was removed,
or no mark if it had passed its checks, and then tasks read the table again. The same holds when
another run's check passed on the new data and cleared the mark before the new run was set back:
the earlier mark comes back with the earlier data.

Sometimes Lakana cannot tell what the earlier data was left with: the table was marked before
Lakana remembered earlier states, or more than eight changes of its state happened in between. The
table then gets a mark that reads `Set back to data whose checks are not known`, readers stay
refused, and the `TABLE_ROLLED_BACK` event says `kept_unknown`. Build the table again in the
project that builds it, so that its checks run again, or run a project that reads it: its check
task runs the table's checks and clears the mark when they pass. A `block` check of a later run
that fails on a table kept in an earlier run sets back only its own write and leaves the mark. The
rules are in [Checks](../reference/checks.md#the-rules).

## What a check sees

- A check runs as the principal that runs the project, an ordinary read query like those you run
  yourself. If a row filter or a column mask applies to that principal, the check does not see the
  rows or values it hides and does not check them. The result then carries a note under its
  line in `lakana runs show`: `checked under a row filter or column mask of NAME: rows and values it
  cannot see were not checked`.
- A check reads the tables as they are at that moment. A check of a table the project builds
  waits for no other table (see [a key that exists in another table](#five-checks-to-copy)).
- Lakana counts the rows of your `SELECT` (it runs `SELECT count(*) FROM (your check)`), so
  something that only your select list would raise, such as a failing cast of a column, does not
  raise. The `WHERE` and the joins decide.
- A check is subject to the time limit and the memory limit of every query. A check that cannot
  run is reported as that and does not pass.

## Limits

Read these before you rely on checks for a table that other people read.

### Readers and timing

- **Readers can see failing data until it is set back or cleared.** With `keep` they see it on
  purpose and for as long as the table is kept (see [Keep](#keep)); only tasks are stopped. A run
  writes the table and then checks it.
  Between the two, for as long as the checks take, anyone who reads the table sees the data that
  is about to be set back. A table can be published only after its checks pass: that does not exist
  yet. What a project can do today is check the tables it only reads (see [A table your project
  only reads](#a-table-your-project-only-reads)). Such a check runs before the assets that read
  the table, so bad data in it is never written into your tables. Keep the checks of the tables
  you build for what can only be known after building.
- **If somebody else writes to the table** between the run's write and its set-back, the table is
  left as it is and the message says so.
- **A schema change the run made goes back with the set-back,** except on a table built for the
  first time (that table is not published instead).
- **The failed data is kept for seven days.** After a set-back, Lakana keeps the failed state of the
  table for seven days, and `lakana check rows` shows the rows from it (see [How long the rows can
  be shown](#how-long-the-rows-can-be-shown)). Maintenance removes it after that, so look at the
  rows while you need them.

### A run that does not end

After a task wrote its table, its checks are tried until they give a verdict. Three situations keep
them from ever giving one:

- A worker is lost in every attempt of the checks, most often because a check needs more memory
  than the worker has. The task shows lost attempts and starts again about once a minute, with
  the default settings. A single lost worker is no problem: another worker takes the task and runs
  the checks again, without building the table again.
- The checks cannot start: the run's principal no longer exists, or the worker is too old to read
  the deployed project version. The task waits to try again, and its message says that the table
  holds data that is not checked yet and names the command that stops the run.
- Counting a check keeps failing for a temporary reason (an object store or a database that does
  not answer). Usually the task's last attempt ends this, with the check `errored`; only a
  project that asks for more than 5 retries reaches the bound below.

Lakana stops by itself after 5 more tries of the checks, counted from the write. The next free
worker then only sets the table back, and the task ends `failed` with the code
`checks_unfinished` (see [Why did it fail?](../reference/why-did-it-fail.md)). Its message names
the cause and then says what became of the table, for example: `The checks of this table did not
finish after its write, nor in 5 more tries: the worker running them was lost each time, the
checks could not start (the run's principal no longer exists, or this worker cannot read the
deployed project version), or counting a check kept failing for a temporary reason. Lakana
stopped trying. The table was set back to before this task ran.` The set-back reads no check, so
a worker that cannot read the project version still sets the table back.

When the run's principal no longer exists, the table cannot be set back either, because the
set-back is a write of that principal. The message then ends with `The table could not be set
back; it may hold data of this run that was not checked.` Run the job again once a principal is
bound, which rebuilds the table, or fix the data by hand.

The run ends `failed`. A job with `notify:` sends its `failed` message, which lists the table
under "Checks never finished" (`checks_unfinished` in the `json` format; see [Get told when a job
fails](get-told-when-a-job-fails.md)). The worker with the scheduler role logs a warning with the
code `checks_unfinished` (see [Log events](../reference/log-events.md)). What to do next:

- Read the attempts with `lakana runs logs RUN_ID --task ASSET --env dev`.
- Lost attempts: raise the worker's memory limit (`LAKANA_WORKER__MEMORY_LIMIT`, or
  `LAKANA_WORKER_MEMORY` in the Compose bundle's `.env`), or make the check read less.
- Checks that could not start: bind a run principal with `lakana deployments set-run-as`, or
  upgrade the worker, as the message says.
- A count that kept failing: check that the object store and the database answer.
- Then run the job again.

You do not have to wait for the 5 tries: `lakana runs cancel RUN_ID --env dev` ends the run at
once, and the table is set back first, as below.

If you cancel a run while a task of it has written its table and the checks have not passed, the
table is set back first: the worker hands the task back, the next free worker sets the table back,
and the task then ends `cancelled`. The same holds when the task was waiting for its checks
(after a lost worker, with the wait reason `retry` or `no_worker`) or its worker was lost; see
[A worker that is lost](#a-worker-that-is-lost). A later `lakana runs resume` of an
incremental asset does not write the same increment twice. A count that a cancel stops ends its
own query as failed, with the cause `cancelled`, and the task ends `cancelled` once the table is
set back.

With a `keep` check, a cancel has two more effects. If you cancel after the task wrote its
table and before its checks ran, the `keep` check that would have marked the table is not run
either, whether the worker is alive (it sees the cancel and hands the task back), was lost, or the
task already waits again for its checks. The table is set back instead (see [A worker that is
lost](#a-worker-that-is-lost)), and nothing marks it. A run
that was cancelled before the check of a kept table ran is taken up again by
`lakana runs resume`, which builds the table.

### Kept tables

- **The mark is not an access control.** It stops tasks of the pipeline. `lakana sql`, the SDK and
  the REST API read a kept table as before, and grants decide who may.
- **Only a run of the project that builds the table clears the mark.** A task of another project
  that reads it is stopped, and a check of another project that reads it cannot run. A project
  that removed the asset must add it back and build it, or drop the table.
- **In rare cases the mark survives a good rebuild.** A rebuild by a version of the project that
  has no checks for the table clears the mark in a step of its own. If the worker stops right
  after the write, that step may not run. The next build of the table clears the mark; until then
  tasks that read the table are stopped, so build it again.

### What a check does not see

- **A check sees what the run's principal sees.** Rows hidden by a row filter are not checked, as
  above.
- **A check proves what its SQL says and nothing else.** A check that reads no row of a column
  cannot find a fault in it.
- **A check that compares two tables of one project** belongs to the one built later, as above.
- **A check of a view has nothing to set back,** and a check of an artifact is refused at deploy.
  A check of a table the project only reads sets nothing back and keeps nothing.

### Numbers

At most 50 checks of one table. Combine checks or split the table.

### Python assets

A Python asset can be checked like any other. Its set-back is stricter about what counts as its
own work:

- The set-back of a Python asset undoes only what this run wrote through the platform's write. If
  anyone else changes the table before, between or after the run's writes, the table is left as
  it is, even when it holds rows of this run that failed a check.
- Between a failed attempt and the next one, the table holds the failed attempt's data,
  unchecked.
- If the worker is lost after the last attempt's write, the next worker sets the table back to
  where the run started (see [A worker that is lost](#a-worker-that-is-lost)). If the table cannot
  be set back, it keeps that data, and the task's message says so.
- A Python asset **without checks** records no baseline, so a lost worker leaves what it wrote,
  whether the task is run again or ends `failed` on its last attempt.
- A Python asset that writes its own table itself is recognised as the writer, as when it returns
  its result: a failed check sets those writes back too. Write it with `client.tables.replace`
  (which also creates it) or `client.tables.append`. A task may neither create nor load its own
  table through the Iceberg REST catalog, so an Iceberg client inside a task has no table to start
  from; an Iceberg commit it is handed counts as its own write only when it adds a snapshot, not
  when it points the table at a snapshot the table already has.

## Where next

- [Checks](../reference/checks.md) has the exact rules: every refusal at deploy, the outcomes, the
  commands, the routes, who may read the results and what is audited.
- [Projects and deploys](projects.md) covers assets, `lakana.yaml` and runs.
- [Stop a query](stop-a-query.md) and [Why is my query or task waiting?](why-is-it-waiting.md)
  say what the queries of the checks look like to the commands that list queries.
