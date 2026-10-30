# Checks

Exact facts about data quality checks: the files, the keys, what a deploy refuses, what a check and
a set-back can end as, the commands, the routes, who may read the results and what is audited.
[Check your data](../how-to/check-your-data.md) teaches them with an example.

## Files and names

| Rule | Value |
|---|---|
| Where | `checks/NAMESPACE/TABLE/NAME.sql` in the project. `NAMESPACE` may have several parts: `checks/silver/eu/readings/no_gaps.sql` checks `silver.eu.readings`. |
| What it holds | One `SELECT` that returns the rows that are wrong, with an optional `/* lakana */` comment at the top. |
| The check's name | `NAMESPACE.TABLE/NAME`, for example `silver.station_temperatures/plausible_range`. The commands show and take this name. |
| Names | The same as for tables: not empty, no `/`, no `.`, not `..`. A Python file under `checks/` is refused; a check is SQL. |
| The table | One that an asset of the project builds, or reads. It must not be an artifact. The SQL must read it. |
| The most | 50 checks of one table. |

`lakana init` writes one check, `checks/silver/station_temperatures/plausible_range.sql`, for its sample asset (`--no-sample` leaves it out).

## The comment

| Key | Values | Default |
|---|---|---|
| `description` | Text that says what the check is for. | None. |
| `on_failure` | `block`: a failing check sets the table back and stops what is built from it. `keep`: a failing check fails the task and leaves the table with the data this run wrote; nothing that reads it is built, and the table is marked `kept` until its checks pass (see [Keeping a table](#keeping-a-table)); allowed on a check of a table the project builds with `write: replace` (SQL or Python) or only reads, and refused at deploy on a check of an asset that appends or merges, or of a view. `warn`: a failing check is reported and nothing else happens. | The project's `checks.on_failure`, else `block` |

Any other key is refused, and so is any other value of `on_failure`; the message lists the right
ones. A file without the comment takes the project's default.

### The default of `on_failure`

The value in force for a check is, in this order: its own `on_failure`, then `checks.on_failure`
in `lakana.yaml` (`block` or `warn`, also in an environment's override; see
[Projects and deploys](lakana-yaml.md#the-default-of-the-checks)), then `block`. A deploy
stores the resolved value on every check, so nothing reads `lakana.yaml` at run time, and
`lakana check list` shows the value that was in force. `keep` cannot be a default.

## What a deploy refuses

A deploy and a dry run read `checks/` and refuse the whole deploy if one check is wrong. Each
message names the file and, where there is one, the line. All problems of a project are reported
together.

| The message says | The cause | What to do |
|---|---|---|
| `a check must live in a directory named after its table: checks/<namespace>/<table>/<name>.sql` | The file is too near the top of `checks/`. | Move it to `checks/NAMESPACE/TABLE/NAME.sql`. |
| `Namespace component ...`, `Table name ...` or `the check's name must be a valid name: ...` | A part of the path is empty, or holds `/`, a dot, or another character a name may not have. | Rename the directory or the file. |
| `a check is a SQL file; a Python file under checks/ is not run. Write the check as a SELECT that returns the rows that are wrong` | A `.py` file is under `checks/`. | Write the check as SQL. |
| `symlinks are not allowed under checks/`, `the path has a non-UTF-8 or control character`, `cannot be read: ...` | The file or directory cannot be used as it is. | Replace the link with the file, rename the path, or fix the permission. |
| `A and B are both the check 'TABLE/NAME'` | Two paths differ only in capitals, so they are the same check. | Rename one of them. |
| `unknown key 'KEY'; a check's /* lakana */ block takes: description, on_failure` | The comment has a key a check does not use. | Remove the key. |
| `on_failure must be block, keep or warn (got 'VALUE')` | The value of `on_failure` is none of the three. | Use `block`, `keep` or `warn`. |
| ``on_failure: keep cannot be used on `TABLE`: it is an incremental asset, and building it again only adds rows, so the rows that failed would stay. Use `on_failure: block`: it takes the failed increment out again.`` | The check says `on_failure: keep` and its table is an asset of `write: append` (or `materialize: incremental`). A kept table clears by being built again, and an append would leave the failed rows in it. | Set `on_failure: block` in the check (or `warn`). |
| ``on_failure: keep cannot be used on `TABLE`: it is written with `write: merge`, and building it again never deletes a row, so the rows that failed would stay. Use `on_failure: block`: it takes the failed merge out again.`` | The check says `on_failure: keep` and its table is an asset of `write: merge`. | Set `on_failure: block` in the check (or `warn`). |
| ``on_failure: keep cannot be used on `TABLE`: it is a view and holds no data to keep. Use `on_failure: block`: for a view it stops what reads it in that run.`` | The check says `on_failure: keep` and its table is an asset of `materialize: view`. | Set `on_failure: block` in the check (or `warn`). A failing `block` check of a view fails the task and nothing downstream is built in that run; the rollback outcome is `not_a_table`. |
| ``checks.on_failure: must be block or warn (got 'keep'). `keep` leaves failing data where readers find it, so it can only be set on a check: put `on_failure: keep` in that check's /* lakana */ block.`` | `lakana.yaml` sets `keep` as the default of the project's checks. In an environment's override the message starts with `environments.NAME.checks.on_failure:`. | Set `block` or `warn` as the default, and put `on_failure: keep` in the comment of each check that should keep its table. |
| `checks.on_failure: Input should be 'block' or 'warn'` | `lakana.yaml` sets another value as the default, for example `stop`. | Use `block` or `warn`. |
| `the check has no SQL` | The file holds only the comment. | Write the `SELECT`. |
| `a check is one SELECT that returns the rows that are wrong; this file has N statements` or `... this file is a DELETE statement` | The file holds more than one statement, or a statement that is not a `SELECT`. | Keep one `SELECT`. |
| A message of the SQL parser, or of the rules for a submitted query, for example `table function X is not allowed ...` or `ATTACH is not permitted in a submitted query.` | The SQL is wrong, or does what no query may: it reads files directly, calls a function a query may not call, holds a statement that changes data (also inside a CTE or subquery: ``A query reads data and cannot change it: ...``), or uses `WITH RECURSIVE` or `CONNECT BY`. | Fix the SQL; register the data as a table and select from it. |
| `the SQL is nested too deeply`, or a message that the SQL is longer than the limit | The `SELECT` is too complex or too long. | Split it into two checks. |
| `the check does not read 'TABLE', the table its path names. Select from that table, or move the file into the directory of the table it checks.` | The `SELECT` does not read the table of its directory. | Select from that table, or move the file. |
| `table 'TABLE' is neither built nor read by this project. A check belongs to a table that an asset of this project produces or selects from. This project builds: ... It reads: ...` | No asset builds the table or reads it. A common cause is a deleted asset whose check is still there. | Move the check to a table the lists name, or add the asset, or delete the check. |
| `'TABLE' is an artifact; a check needs a table. Check a table the artifact is built from instead.` | The directory names an artifact. | Check the table the artifact is built from. |
| `more than 50 checks of table 'TABLE'; combine checks or split the table` | The directory has too many files. | Combine checks. |
| ``more than 50 checks of table 'TABLE' (2 of them generated from the asset's header); remove or combine hand-written checks, or split the table`` | The hand-written checks and the [generated checks](#generated-checks) of the table together are more than 50. | Remove or combine hand-written checks. |
| ``A check name may not end in ` (generated)`, which marks a check generated from an asset's header; rename `checks/…`.`` | A file under `checks/` is named like `unique_id (generated).sql`, in any letter case. `lakana check list` would show it as a generated check. | Rename the file. |

A project without `checks/` deploys and runs as before.

## Generated checks

An asset's header can declare `key:` and `references:`
([Model fields](asset-front-matter.md#model-fields-kind-grain-key-and-references)). A deploy turns
them into checks of the asset's table, with no file under `checks/`:

| Check | From | Returns |
|---|---|---|
| `TABLE/key_unique` | `key:` | Every row whose key, all key columns together, appears in more than one row. |
| `TABLE/key_not_null` | `key:` | Every row with a null in any key column. |
| `TABLE/ref_COLUMN` | each entry of `references:` | Every row whose `COLUMN` is not null and has no match in the target's key column. A null is allowed; a reference that is required is also in `key:`. |

`key: none` generates nothing. Apart from where they come from, they are checks like any other:
they run after the table is written, end as the outcomes below, set the table back when they
block, and `lakana check rows RUN TABLE/key_unique --env ENV` shows the rows they found.

| Rule | Value |
|---|---|
| The source | The asset's file and the line of its header, for example `assets/gold/fact_orders.sql:1`. A result's `path` holds it where a hand-written check's holds `checks/…`. |
| `on_failure` | `checks: key:` or `checks: references:` in the asset's header, then `checks.on_failure` in `lakana.yaml`, then `block` (as in [The default of `on_failure`](#the-default-of-on_failure)). `keep` follows the rules of a hand-written check: never on a view, an append or a merge. |
| The most | They count toward the 50 checks of a table, so a table near the limit can be refused when its header gains `key:` or `references:`. The message says how many of them the header generated: ``more than 50 checks of table 'gold.fact_orders' (3 of them generated from the asset's header); remove or combine hand-written checks, or split the table``. |
| Names | A hand-written check of the same table may not take a generated name: ``A check named `key_unique` is generated for `gold.fact_orders`; rename `checks/gold/fact_orders/key_unique.sql`.`` Rename the file. A hand-written `key_unique` of a table that declares no `key:` is fine. |
| The mark | `lakana check list`, `lakana table health`, `lakana runs show`, `lakana run` and the Model block of `lakana table get` show `key_unique (generated)`, and the list of checks of `lakana deploy` shows `key_unique (generated)` too; REST and the SDK have `generated: true` on the result (`false` on a hand-written check, and in the SDK `false` from an older server), and a deploy's list of checks has `"generated": "key"` or `"generated": "references"` on such a check. |
| Who sees a reference check | A `ref_<column>` result is shown only to someone who may read the reference's target. Anyone else gets a count instead: `1 check of a reference to a table you cannot read is not shown.` in `lakana table health`, `lakana check list`, `lakana runs show` and `lakana run`; `hidden_checks` on table health and on each task of a run, `hidden` on a page of `GET .../checks`. `GET .../runs/{id}/checks` leaves such results out (the run's tasks carry the count), and `lakana check rows` answers as for a check the run does not have. A task's `error_message` (in `lakana runs show`, `lakana run` and each attempt of `lakana runs logs`) is rebuilt without such a check and its rows, and is empty when nothing else is left in it. A kept or not-published table's note (`state_message` of `lakana table get`, and the refusal of a read of a table that is not published) names only the checks you may see; a note whose only failing check is withheld names none. The free-text task log (`lakana runs logs`) is one text for every reader, so it gives a reference check no outcome and no row count (`check ref_<column>: done; see lakana runs show`), and its `error:` line leaves reference checks out. A task is never told of a reference check (a refusal of a kept table, say): what a task is told ends up in its own message, which every reader of the run reads. Notifications never name one: they say `A reference check failed; see lakana runs show …`. The `CHECK_FAILED` audit event names the check and its row count; only someone who may read the audit log of the whole instance reads it. A result whose target cannot be told (its project version is gone) is shown only to someone who may read every table of the environment. Key and hand-written checks are not affected. |

### A reference sets the run order

A reference target that the project builds is built before the table that references it, like a
table its `SELECT` reads:

- `lakana run --select gold.fact_orders --upstream` also builds the tables `gold.fact_orders`
  references.
- When the target fails, or a blocking check sets it back, the tables that reference it are not
  built; they end `upstream_failed`, as after any failed table.
- This holds whatever `checks: references:` says: with `warn`, a failed reference check does not
  stop the table, but a failed target still does, because the table is built after it.
- Two assets that reference each other, directly or through others, are a cycle, and the deploy
  refuses it; remove one of the references, or check it with a hand-written check under
  `checks/`, which adds no step to the run.

A target the project does not build adds no step to the run; its check reads it as it is.

### Before a generated check counts

A deploy does not know the types of a table's columns, and it does not know the columns of a
`SELECT *` or a Python asset at all. So before it counts a generated check, the worker compares
the columns and their types in the catalog, as `lakana table get` shows them. A problem makes the
check `errored`, with one of these texts in `error`; a `block` check that errored sets the table
back, as a failed one does.

| `error` says | The cause | What to do |
|---|---|---|
| ``Key column `id` is not a column of `gold.fact_orders`; fix `key:` in assets/gold/fact_orders.sql:1 and deploy again.`` | The written table has no such column. When it has one that differs only in letter case, the text adds ``Column names match exactly; the table has `ID`.`` | Fix `key:`, or the `SELECT`, and deploy again. |
| ``Key column `price` is double, a floating-point type, so it cannot identify a row; cast it in the asset, for example to DECIMAL, BIGINT or VARCHAR, and deploy again.`` | A key column is `float` or `double`. A merge on such a key ran in earlier releases; its key checks now error at every run. | Cast the column in the asset (in SQL, `CAST(price AS DECIMAL(18, 4))`, `BIGINT` or `VARCHAR`), or key on another column, and deploy again. |
| ``Reference column `store_id` is not a column of `gold.fact_orders`; fix `references:` in assets/gold/fact_orders.sql:1 and deploy again.`` | The written table has no such column (the letter-case hint as above). | Fix `references:`, or the `SELECT`, and deploy again. |
| ``The reference `store_id` at `assets/gold/fact_orders.sql:1` cannot be checked: its target is missing, not readable by the run's principal, or no longer has the key column; fix the reference or the grant and deploy again.`` | The target table is gone, the run's principal may not read it, or it no longer has the referenced column. The text does not say which, so that it tells nothing about a table its reader may not read. | Check that the target exists and has the column, and grant the run's principal `READ_TABLE` on it. |
| ``The reference `store_id` at `assets/gold/fact_orders.sql:1` has a type that differs from its target key column's; make the types match and deploy again.`` | The two columns have different types. Only `int` and `long` match each other; every other type must be the same, so `decimal(10,2)` against `decimal(12,2)`, or `string` against `long`, differ. The text names neither type. | Cast the column in the `SELECT` to the type that `lakana table get` shows for the target column. |

When the count of a reference check fails under a row filter or column mask, its `error` names the
checked table only if the filter or mask is on that table; one on the target reads ``… under a row
filter or column mask on one of the tables; …``, because whoever reads the result may not be
allowed to read the target.

## What a check can end as

Each check of a run has one result.

| Outcome | Meaning |
|---|---|
| `passed` | The `SELECT` returned no row. |
| `failed` | A check of `block` or `keep` returned rows. The number is in `failing_rows`. |
| `warned` | A check that only warns returned rows. |
| `errored` | The check could not be counted (a temporary failure, such as an object store or a database that could not be reached, is not `errored` at once: the task is tried again on its next attempt, with the wait reason `retry` (see [Why is my query or task waiting?](../how-to/why-is-it-waiting.md#a-waiting-task)), and only a failure on the last attempt is recorded): its SQL failed, it ran past the time limit of a query or out of memory, or the run's principal may not read a table it names. A check of `block` or `keep` that errored counts as failed. `error` says why, with the query engine's error type when it names one and a fixed sentence, and never a value from the table. |

A blocking check that failed or errored sets the table back. A `keep` check does not: its result has no rollback outcome and the table keeps the data (see [Keeping a table](#keeping-a-table)). What a set-back did is the *rollback
outcome* of the result, and the group in `lakana runs show` reads it as a phrase:

| Rollback outcome | The group reads | What happened |
|---|---|---|
| `rolled_back` | `rolled back` | The table is back at the state it had before the run. |
| `unpublished` | `not published` | The run built the table for the first time; it is registered and a read is refused. |
| `nothing_written` | `nothing was written` | The run wrote no rows, so there was nothing to set back. |
| `not_a_table` | `a view, nothing to set back` | The asset is a view. |
| `changed_since` | `left as it is, changed since` | Somebody else wrote to the table in the meantime; Lakana did not touch it. |
| `failed` | `rollback failed` | The set-back did not work; the table holds the run's data. |

A result has no rollback outcome when the check passed or warned, and for a table the project only
reads.

## Keeping a table

A check with `on_failure: keep` fails the task and leaves the table as the run wrote it. The
table gets a *mark*: the run, the task and the check (`kept_run_id`, `kept_task_run_id`,
`kept_check` on the table; the first check by name, followed by `and N more` when several
failed). While the mark stands, every task of the project that builds the table is stopped
before it reads the table, except as the rules below say, and ad-hoc readers (`lakana sql`, the
SDK, the REST API) read the table as before. A table has one of three states: `published`,
`not_published` (a first build failed a `block` check) and `kept`.

### The rules

- **Who sets the mark.** Only a task that built the table, when a `keep` check of that table
  fails. A `keep` check of a table the project only reads fails its task and stops the assets
  that read it in that run, and sets no mark. A `keep` check that could not run because a table
  of another project that it reads is kept sets no mark either; its task fails and its message
  says the checks could not run. One exception: when the table the task builds already carries a
  mark, the build has replaced the data that mark names, and no check could run on the new data.
  The mark then moves to the new run and names the check as one that could not run, and the
  table's state text says it holds data no check could run on. Readers stay refused.
- **Where `keep` is allowed.** On a check of an asset of `materialize: table` (SQL or Python)
  and of a table the project only reads. A deploy refuses it on a check of an asset that appends
  or merges (building it again never deletes a row, so the failed rows would stay) and of a view
  (it holds no data); see [What a deploy refuses](#what-a-deploy-refuses).
- **Who clears it.** A run of the project that builds the table, in one of three ways: the
  table's own task passes all its `block` and `keep` checks after a rebuild; the check task of
  a later run passes (see below); or a project version that has no checks for the table rebuilds it.
  Each clearing writes one `TABLE_KEPT_CLEARED` event (`why`: `checks_passed` or
  `rebuilt_without_checks`). In rare cases (the worker stops right after the write) the mark
  survives a good rebuild, and the next build of the table clears it.
- **A set-back follows the mark.** A table remembers the states it had before the current one
  (`kept_earlier`, newest last, at most the newest eight), each with the snapshot it was a verdict
  on: a mark, or "no mark" when the data had none. A mark that moves to a newer run (a `keep`
  check that fails again, or a rebuild whose checks could not run) puts the state it replaces on
  that stack, and so does a clear: a passing check phase, a later run's check task or a rebuild
  without checks puts the mark it removes on the stack, with the snapshot that mark was a verdict
  on, and records the snapshot its own checks judged. When a set-back moves the table to a
  snapshot that the table's current verdict does not describe, the table takes back the newest
  state the stack holds for that snapshot, in the same step. That happens whoever set the current
  state: the run that wrote the data can be cancelled after another run's check judged its data
  and cleared the mark, and the table then has the earlier data and, again, its earlier mark. The
  entries above the one used describe data newer than the snapshot, which is gone, so they go.
  `lakana table get` and the refusal of a reader name the run whose data the table holds again,
  with the check and its `could not run` wording as they were. If that state is "no mark", the data
  had passed its checks or had none to pass, the table has no mark afterwards, and readers are let
  in again; only then. If the stack has no state for the snapshot (the history ran out of eight
  entries, or the table was marked before upgrading to migration `0031`), nothing says whether the data passed its checks, so the table gets a mark that says the checks
  are not known: it names the run that was set back, refuses readers like every mark, and is
  cleared by the next passing checks like every mark. When the verdict already describes the
  snapshot the table goes back to, nothing changes. A set-back of data a mark does not name (a
  `block` failure of a later run after the table was kept in an earlier one) leaves that mark; a
  first build has nothing to go back to and keeps its mark. A table that has no verdict (it was
  never marked, or was cleared before migration `0031`) is left as it is by a set-back. A set-back also
  acts on a mark that names another run when that mark is the verdict on the data the set-back
  removes (two writers set back in the reverse order of their writes). The set-back records which of these happened in
  the `TABLE_ROLLED_BACK` event (see [What is audited](#what-is-audited)). A table that was kept
  once and then cleared keeps the snapshot of its latest verdict, which a passing check phase of a
  later build refreshes (the state it replaces goes on the stack), so a set-back of a later write
  finds the verdict of the data before it.
- **A later run.** A run that builds an asset which reads a kept table without rebuilding it
  gets a check task for the kept table (kind `source_check`, see [SDK and REST](#sdk-and-rest)).
  It runs the table's checks before any reader. If they pass, the mark is cleared and the
  readers are built; if one fails or cannot run, the task fails, the readers are not built, and
  nothing is set back. If the project has no check for the table any more, the task fails too:
  only building the table again clears the mark. A reader that only reaches the table through views is covered too; a table
  asset that is not built stops the search, because it holds older data that was checked when it
  was built.
- **Which tasks read a kept table.** In a run of the project that builds the table: the table's
  own task (its checks, its rebuild) and every task that comes after it. Any other task is
  refused. A task of **another project** is always refused, also when that project has a check
  of its own on the table and the check passes, because its check may be weaker; only a run of
  the project that builds the table clears the mark. The count of a check may read a kept table
  of its own project.
- **A table whose asset was removed** from the project has no project that builds it, so no run
  clears the mark. Add the asset back and build it, or drop the table
  (`lakana table delete TABLE --env ENV`). Ad-hoc reads work throughout, and no command clears
  the mark by hand.
  A deploy or dry run that removes such an asset says so in its `warnings` (the SDK's
  `Deploy.warnings`, the REST `DeployOut.warnings`), and `lakana deploy` prints each as a
  `warning:` line on stderr; it lists only tables you may read, and it is not a refusal.
- **`lakana runs resume`** of a run in which the check task of a kept table failed builds that
  table again, also when the run was cancelled before the check ran.
- **A cancel** while a worker runs the checks can leave kept data without the mark, as a cancel
  leaves data that was not checked; a later build of the table sets or clears it. When the
  worker was lost, or the task waits for its checks again after a lost worker, the next worker
  sets the table back instead.

### The messages

| Where | The message | Cause | What to do |
|---|---|---|---|
| The task's message of a failed `block` or `keep` check, of a check task of a kept table, and the message of a table that is not published: one sentence for each failing check that read a snapshot (at most three), before the closing sentence | ``Look at the rows: `lakana check rows RUN CHECK --env ENV`.`` | The check read a snapshot of the table, so the rows it found can still be shown. A check that read none (a view, or SQL that does not read its table) gets no such sentence. | Run the command. |
| The task's message, after the sentence for each failing check | ``The table keeps this run's data (`on_failure: keep`), and nothing that reads it was built. Repair the data or the asset; after a fix, deploy and run again; if the project did not change: `lakana runs resume RUN --env ENV`.`` | Only `keep` checks failed or errored. | Look at the rows with `lakana check rows` or with the check's own `SELECT` (the table still holds them), repair the data or the asset, and run again. |
| The task's message, after the sentence for each failing `keep` check | ``The table was dropped while its checks ran, so nothing is kept.`` | A `keep` check failed and the table was dropped before the mark could be set. No mark is set and no rows are shown, because the table is gone. | Nothing to clear. Build the table again if you still want it. |
| `lakana table get`, `state_message` of the table | ``Kept after a failed check (`CHECK`, run RUN): the table holds the data that failed. Tasks do not read it until its checks pass.`` `CHECK` is the first failing check and ``and N more``; checks of a reference to a table you may not read are left out, and with none left the note names no check: ``Kept after a failed check (run RUN): …``. | The table carries the mark. | Repair the data or the asset and build the table again. |
| `lakana table get`, `state_message` of the table, after a rebuild whose `keep` check could not run | ``Kept after a check that could not run (`CHECK`, run RUN): the table holds data no check could run on. Tasks do not read it until its checks pass.`` | The table carried a mark, was built again, and every check that did not pass was refused a kept table of another project, so no check ran on the new data. The mark names the new run. | Clear the table that is kept, then build this table again. |
| `lakana table get`, `state_message` of the table, after a set-back to data whose checks are not known | ``Set back to data whose checks are not known (run RUN was set back): no passing check has judged the data the table holds. Tasks do not read it until its checks pass.`` | A run's data was set back and the table's history has no verdict for the data it holds again (the history ran out, or the table was marked before the history existed). `RUN` is the run that was set back. | Build the table again (`lakana run --select TABLE --env ENV`) in the project that builds it, so that its checks run again; or repair the data and run that project. |
| The message of the check task of a later run, after the sentence for each failing check | ``` `TABLE` was kept after a failed check in run RUN and still fails, so the assets that read it were not built. Build it again (`lakana run --select TABLE --env ENV`), or repair the data and run again.``` | The kept table still fails its checks. When the mark names a check that could not run, the text reads `` `TABLE` was kept because its checks could not run in run RUN, and now fails a check, so the assets that read it were not built.`` and then the same advice. With a mark of unknown checks it reads `` `TABLE` was set back to data whose checks are not known (run RUN was set back), and now fails a check, so the assets that read it were not built.`` and then the same advice. | As the message says. |
| The message of a task that is refused a kept table, and the `error` of its query run | ``` `TABLE` was kept after a failed check (`CHECK`, run RUN) and has not passed its checks since, so this task does not read it. A run of the project that builds `TABLE` clears this: build it again there (`lakana run --select TABLE --env ENV`), or repair the data and run that project. If no project builds it any more, drop it: `lakana table delete TABLE --env ENV`.``` `CHECK` never names a reference check, which every reader of the task's run would read; with no other check the text names none (``was kept after a failed check (run RUN)``). The REST API answers 409, the Iceberg REST catalog's load route 404. When the mark names a check that could not run (a rebuilt table whose `keep` check read another project's kept table), the first sentence reads `` `TABLE` was kept after a check that could not run (`CHECK`, run RUN) and has not passed its checks since, so this task does not read it.`` and the rest is the same; the status is 409 as well. For a table set back to data whose checks are not known the first sentence reads `` `TABLE` was set back to data whose checks are not known (run RUN was set back) and has not passed its checks since, so this task does not read it.`` and the rest is the same; the status is 409 as well. | The reader is a task of another project, a task that has no place after the table's own task in its run, or a run that started before the table was kept. | Run the project that builds the table. From another project, wait for that. If no project has the table as an asset any more, add the asset back and build it, or drop the table. |
| A `warning:` line of `lakana deploy`, and `warnings` of the deploy | ``this deploy removes the asset of `TABLE`, which is kept after its checks failed or could not run, or set back to data whose checks are not known; add it back and build it, or drop the table`` | The deploy's diff removes an asset whose table carries the mark. No project builds the table any more, so tasks that read it stay refused. | Add the asset back and build it (`lakana run --select TABLE --env ENV`), or drop the table (`lakana table delete TABLE --env ENV`). |
| The message of the check task of a kept table that the project has no check for any more, and the `error` of its one result (`TABLE/(none)`) | ``` `TABLE` was kept after a failed check in run RUN, and the project has no check for it any more, so nothing can pass. Only building it again clears this: `lakana run --select TABLE --env ENV`.``` With a mark that names a check that could not run, ``was kept after a check that could not run in run RUN`` takes the place of ``was kept after a failed check in run RUN``. With a mark of unknown checks, ``was set back to data whose checks are not known (run RUN was set back)`` takes its place. | The project removed the table's checks after the table was kept, and the run builds only what reads the table. No check can pass, so the readers are not built. | Build the table again with `lakana run --select TABLE --env ENV`: a build by a version without checks for the table clears the mark. |
| A check's `error` | `a table this check reads was kept after its checks failed or could not run, or was set back to data whose checks are not known, and has not passed its checks since, so the check could not read it. `lakana table get` on each table the check reads shows which one, and why.` | The check reads a kept table of another project. (A check may read a kept table of its own project.) The check counts as errored and acts by its own `on_failure`. | Run `lakana table get` on the tables the check reads, and clear the one that is kept. |
| The task's message when no `keep` check could run | ``The checks of `TABLE` could not run, so nothing that reads it was built. After the kept table that a check reads is cleared, run again; if the project did not change: `lakana runs resume RUN --env ENV`.`` | No check of the table failed, and every check that did not pass was refused a kept table of another project. The task's own table gets no mark, unless it carried one already (see the state text above). In the check task of a kept table this follows the sentence of each check that could not run. | Clear the table that is kept, then run again. |

## Commands

### `lakana check list`

Lists check results, newest first.

| Option | Meaning |
|---|---|
| `--env ENV` | The environment. Default: the profile's. |
| `--run RUN_ID` | Only the results of this run. |
| `--table NAMESPACE.TABLE` | Only this table's results. With `--run`, the filter is applied on your machine. |
| `--limit N` | At most N results, 1 to 1000. Default 50. |

The columns are `run` (the first 8 characters of the run id), `table`, `check`, `outcome`, `rows`,
`on_failure` and `finished_at`; `check` reads `key_unique (generated)` for a
[generated check](#generated-checks); a narrow terminal leaves `on_failure` and `run` out first, and the
line under the table says so. `--output json` has every field, including the whole run id, the
error, the rollback outcome and the note about policies. A run without results prints
`Run RUN_ID has no check results.`

### `lakana check rows`

`lakana check rows RUN CHECK --env ENV` shows the rows a check found in a run, as they were then,
also after the table was set back or when it was never published. `RUN` is the whole run id.
`CHECK` is the check's name, `NAMESPACE.TABLE/NAME`, as `lakana check list` shows it; quote it in
the shell if it holds a space or a quote. The messages of a failed task print the command with the
name already quoted. For a name with a non-ASCII letter, a backslash or a control character, or
one longer than 200 characters, a message cannot print the name as it is, so it prints `CHECK`
and says to copy the name from `lakana check list`.

| Option | Meaning |
|---|---|
| `--env ENV` | The environment. Default: the profile's. |
| `--out FILE` | Write every row to this file instead of printing. |
| `--format FORMAT` | `parquet`, `csv` or `arrow`. Default: from the extension of `--out`. |
| `--max-rows N` | The most rows to display. Default 50. |
| `--timeout SECONDS` | How long to wait for the query. |

The output is that of `lakana sql`: a table, then the number of rows. The details are in
[The rows of a check](#the-rows-of-a-check).

### `lakana table maintain`

For a table with failed checks, the line of `lakana table maintain` has one more part:
`Maintained ENV/TABLE: expired 2 snapshots, deleted 4 files (2048 bytes), removed 1 tag of a failed
check older than 7 days` (`removed 2 tags of failed checks` for several). A dry run prints
`Dry run for ENV/TABLE: would expire 2 snapshots, would delete 4 files (2048 bytes), would remove
1 tag of a failed check older than 7 days`. The part is left out when no tag was removed. See [The tag of a failed
check](#the-tag-of-a-failed-check).

A maintenance run stops at the first safe point after `LAKANA_RUN__MAINTENANCE_TIMEOUT` (30
minutes by default). It then fails with the cause `timeout`, and the table stays
consistent; what it had expired or deleted stays so, and running `lakana table maintain` on the table
again continues where it stopped. See [A maintenance run past its time
limit](../how-to/stop-a-query.md#a-maintenance-run-past-its-time-limit).

### `lakana runs show` and `lakana run`

After the table of tasks, both print one group for every task with checks. A group is a heading
line and a line for each check:

<!-- output -->
```text
silver.station_temperatures   FAILED   2 of 3 checks passed, rolled back
  has_rows                    passed
  no_missing_station          FAILED   1 row
  plausible_range             warned   1 row
```

The heading holds the table, its state, how many of its checks passed (a warning counts as
passed), and the rollback phrase. For a table the project only reads it ends with
`not built by this project`. For the check task of a table that was kept after a failed check
it ends with `kept after a failed check`. For a check task of a run whose project version is gone,
which cannot tell the two, it ends with `check task`. The state is `FAILED` when a blocking check failed or errored,
`warned` when a check warned or a warning check errored, and `passed` otherwise. A check line is
`passed`; `FAILED` or `warned` with the number of rows; or `ERRORED` (`errored` for a warning
check) with the error. A `keep` check reads `failed (kept)` with the number of rows, or
`ERRORED (kept)` with the error; it has no rollback phrase. A result that was counted under a row filter or column mask has a line
`note: checked under a row filter or column mask of NAME: rows and values it cannot see were not
checked` under it. A check generated from the asset's header reads `key_unique (generated)`. A run without checks prints nothing more than it did before.

### Other commands

- `lakana deploy --dry-run` and `lakana deploy` print `Checks:` and, for each table, its checks
  with `(generated)` after one generated from the asset's header, `(warn)` after one that only
  warns and `(keep)` after one that keeps. When
  `lakana.yaml` sets the default of some checks, a line follows: ``2 checks take `on_failure:
  warn` from lakana.yaml.`` (`1 check takes` for one).
- `lakana table get` and `lakana table list` show a `state` of `published`, `not published` or
  `kept`, and `table get` says why for the last two.
- `lakana runs cancel` ends a run whose checks cannot finish; see Limits in
  [Check your data](../how-to/check-your-data.md#limits). A task whose table may hold rows no
  check has passed is set back first, so the cancelled run ends only once a pipeline worker has
  done that; see [A worker that is lost](../how-to/check-your-data.md#a-worker-that-is-lost).
- `lakana query cancel` refuses the query of a check and names `lakana runs cancel`; see
  [Stop a query](../how-to/stop-a-query.md).

## The rows of a check

A result holds a count and never a row. `lakana check rows` makes the rows from the check's SQL.

**How it works.** The command queues an ordinary query of yours whose text is the check's SQL,
taken from the project version the run used. The query is tied to the check's result. When it
runs, the worker reads the checked table at the snapshot the check read, and every other table
the SQL names as it is now, under your own grants, row filters and column masks. Nothing in the
request names a snapshot.

**What the rows are.**

- The rows the check found in that run, as they were then, also after the table was set back, and
  also for a table that was never published.
- Only the columns that the table still has in the same form: under the same name, with the
  same type, and as the same column (not one dropped and added again under the name). A column that the run
  added and that was set back, or that was dropped or changed since, is not shown, because a column
  mask can only be set on a column the table has now. A first build that is not published keeps
  its own columns, so all of them are shown.
- A row filter of yours is applied to the snapshot's rows. A column mask of yours is applied to
  the column it names. A row filter, or a mask's expression, that reads a column which is not the
  same column in the snapshot and in the table now stops the read with a message: Lakana never
  leaves a filter out to make a read work. A mask on a column that is not shown is skipped,
  since nothing is shown of it.
- A check that passed is queued like any other and returns no rows.

**Who may ask.** The right to read the run (`READ_ENVIRONMENT` on the environment, or `RUN`) and
`SUBMIT_QUERY` on the environment; the roles `ANALYST` and above have both. A caller who lacks
them is refused before Lakana looks at any run or check, so the refusal says nothing about whether
they exist. Then, once the check is found, the right to read every table the check's SQL names
(`READ_TABLE`): the SQL becomes the text of the caller's query, so a caller without it is refused
(403) before any other answer about the check, with a message that names none of the tables and
not their number. The worker checks the right to read the tables again when it runs the query, as
for every query.

**The check's SQL in a project version.** `lakana project-version show` (`GET
/api/v1/environments/{env}/project-versions/{id}`) shows a check's `sql` and `references` only to a reader who
may read every table the SQL names, read again from the SQL itself. For anyone else the entry keeps
`name`, `table`, `check`, `path`, `on_failure` and `description`, and `hidden_references` counts
the tables they may not read (0 when nothing is hidden).

**In the audit log.** The `QUERY_COMPLETED` event of such a query has, next to `sql_text`,
`tables_touched`, `row_count` and `status`, the field `check_result_id`: the id of the check
result whose rows were asked for. The event of every other query does not have the field. An
administrator can so tell a read of a table's failed or earlier state from an ordinary query of
the table.

**A task cannot ask.** A principal that is a task of a run, or a token bound to a task, is refused
with ``A task cannot read the rows a check found: they are shown to a person's own query, never to
a pipeline. Run `lakana check rows` yourself, outside the task.`` The REST API answers 403.

### How long the rows can be shown

| Check | Time |
|---|---|
| A failed `block` check | Seven days: `lakana table maintain` removes the snapshot once it is more than seven days old. Exactly seven days old stays, older goes. Maintenance has to run; a table that nobody maintains keeps the snapshot. |
| A `keep` check, a `warn` check, or a `block` check that passed | As long as the table's history keeps the snapshot: ordinary maintenance expires it by `--keep-days` (7) and `--keep-last` (5), like any snapshot. |

### The tag of a failed check

A set-back keeps the failed state of the table under a snapshot tag named
`lakana-check-failed-<run id>`, so that maintenance does not take it away at once. A tag has no
creation time, so its age is the age of the snapshot it names, which is older than the tag by the
time the checks took.

- One run of `lakana table maintain` removes a tag whose snapshot is more than seven days old, and
  the snapshot with it, whatever `--keep-days` and `--keep-last` say, unless another reference
  (a branch, another tag) names the snapshot, a remaining branch needs it as an ancestor, or a run
  still in progress needs it to set its table back. Such a snapshot stays without the tag, and a
  later maintenance run expires it by the ordinary rules once that run has ended.
- Nobody can create a ref with the same prefix, `lakana-check-failed-`, through the Iceberg REST
  catalog: a commit that sets one is refused (below), and Lakana's own set-back is the one setter.
  A table that an earlier release left with such a ref made by hand keeps it, and `lakana table
  maintain` removes it after seven days as before.
- The maintenance report lists the removed tags as `tags_removed` (names; empty when none), and so
  does the `TABLE_MAINTAINED` event in the audit log. A dry run lists the tags it would remove.
- A table that was never published stays not published when its tag and snapshot go; the next run
  whose checks pass publishes it as before.

### When it cannot show rows

The first seven messages come back at once as the error of the command: 409 on the route and
`lakana.errors.Conflict` in the SDK, except the fifth, which is 404 and `lakana.errors.NotFound`.
The others are the failure of the query, after it was queued.

| The message | Cause | What to do |
|---|---|---|
| ``This check has no rows to show from run RUN: `TABLE` is a view, so no snapshot was checked. Run the check's SQL yourself: `PATH` in the project.`` | The checked table is a view. | Run the SQL in `PATH` with `lakana sql`. |
| ``This check has no rows to show from run RUN: its SQL does not read `TABLE`, so no snapshot of it was checked. Run the check's SQL yourself: `PATH` in the project.`` | The check's SQL does not name the table it belongs to. | Run the SQL in `PATH` with `lakana sql`. |
| ``This check has no rows to show from run RUN: the check could not run. Its error is in `lakana check list --run RUN --env ENV`.`` | The check's result is `errored`. | Read the error and fix the check or its table. |
| ``This check has no rows to show from run RUN: no snapshot of the table was checked.`` | The result names no snapshot and none of the three reasons is known. | Run the check's SQL yourself with `lakana sql`. |
| ``Run RUN has no result for the check `CHECK`. Its checks: `lakana check list --run RUN --env ENV`.`` | The run has no check of that name (404). | Copy the name from the list. |
| ``The project version of run RUN is no longer stored, so the check's SQL is not known. The number of rows remains: `lakana check list --run RUN --env ENV`.`` | The project version of the run was removed (409). | The count is in `lakana check list`. |
| ``The SQL of this check stored with run RUN cannot be read, so its rows cannot be shown. Deploy the project again (`lakana deploy --env ENV`) and run it; the number of rows remains: `lakana check list --run RUN --env ENV`.`` | The check's SQL stored in the project version cannot be parsed: it was changed after the deploy (409). No query is queued and nothing of the SQL is shown. | Deploy the project again and run it. |
| ``The rows of this check can no longer be read: the snapshot it checked is no longer in `TABLE`. Maintenance removes a failed snapshot after 7 days, and a table that is dropped and built again starts anew. The number of rows the check found remains: `lakana check list --run RUN --env ENV`.`` | Maintenance removed the snapshot, or the table was dropped, renamed or built again. Also when maintenance removed it while the query was already reading it: a query that fails is asked once more, and this is the text when the snapshot is gone. | The count is in `lakana check list`. |
| ``The check result this query reads is no longer there: it was recorded again by a new attempt of its task, or removed with its run. Run `lakana check rows` again.`` | Before the query ran, the check's task was started again and recorded its results anew, or the run and its results were removed. | Run the same command again: it shows the rows of the new result, or says that the run has no such check. |
| ``The column `COLUMN` of the checked snapshot is not shown: `TABLE` does not have it in this form any more (it was added by the run that failed, or dropped or changed since), so the rows this check found cannot be listed with it. The number of rows the check found remains: `lakana check list --run RUN --env ENV`.`` | The check's SQL names a column that is not shown, in any letter case and also through a table alias (`o.note`). | The count is in `lakana check list`. |
| ``None of the columns of the checked snapshot is in `TABLE` in the same form any more, so the rows this check found cannot be listed. The number of rows the check found remains: `lakana check list --run RUN --env ENV`.`` | The table was built again with other columns. | The count is in `lakana check list`. |
| ``A row filter or column mask of yours on `TABLE` uses the column `COLUMN`, which is not the same column in the checked snapshot and in the table now (it was added, dropped or renamed since, or another column took its name), so the rows this check found cannot be shown under it. The number of rows the check found remains: `lakana check list --run RUN --env ENV`.`` | A row filter or a mask expression of yours reads a column that is not the same then and now. | Ask whoever manages your policies to adjust it. |
| ``The rows of this check cannot be read from `TABLE`: the check result is not one of this table as it exists now, or was removed with its run.`` | The result is not one of the live table (the table was dropped and built again, or the result is of another table). | The count, if the result still exists, is in `lakana check list`. |

## SDK and REST

| What | SDK | Route |
|---|---|---|
| The results of one run | `client.checks.for_run(env, run_id)` | `GET /api/v1/environments/{env}/runs/{run_id}/checks` |
| The rows a check found in a run | `client.checks.rows(run_id, check, environment=env)`; wait with `.wait()`, fetch with `.result()` | `POST /api/v1/environments/{env}/runs/{run_id}/checks/rows` with the body `{"check": "silver.orders/unique_id"}` |
| The results of an environment | `client.checks.list(env, table=..., limit=..., cursor=...)` for one page, `client.checks.iter(env, table=...)` for all | `GET /api/v1/environments/{env}/checks?table=&limit=&cursor=` |

The second route returns `{"items": [...], "next_cursor": ...}`, newest first, with `limit` from 1
to 200 (default 50). Send the same `table` with every page.

A result has these fields: `run_id`, `task`, `name`, `check`, `path`, `table`, `table_id`,
`on_failure`, `outcome`, `failing_rows`, `error`, `checked_snapshot_id`, `rollback_outcome`,
`policy_applied`, `policy_note`, `query_run_id`, `started_at`, `finished_at` and `generated`
(`true` for a [generated check](#generated-checks)). A result holds
a count and never a row of the table.

Other objects carry checks, too: a run's tasks have `kind` (`asset`, or `source_check` for a
table the project only reads, and for the check task of a kept table), `kept_check` (`true` for
the check task of a table that was kept after a failed check, `false` otherwise, `null` for a
check task of a run whose project version is gone, which cannot tell; the commands use it to tell
the two apart and print `check task` for `null`), `setting_back` (`true` while the task has not ended and its next
claim only sets its table back: its worker was lost after it may have written, or a cancel found
it waiting with rows no check has passed; `false` once it has ended; see [A worker that is
lost](../how-to/check-your-data.md#a-worker-that-is-lost)) and `checks`; a deploy has `checks`
(table to a list of the check names and their `on_failure`); a table has `state` (`published`,
`not_published` or `kept`) and `state_message`. A read of a table that is not published answers
409 on the platform routes, and "not found" with the same text on the Iceberg REST catalog's load
route; so does a task's read of a kept table that it may not read. The answer to a commit or a
create on the Iceberg REST catalog carries the table's history (its snapshots, its references,
among them the tag of a failed check, and its properties) only for a caller who may read the
table, as its load would; anyone else gets the new metadata location and the table's current
schema, partitioning and sort order, and the commit itself lands all the same. Such a client can
commit once with each table object it holds: its next commit needs a fresh load of the table,
which requires `READ_TABLE`. A refused commit tells such a caller nothing of the table either
(below). A deploy's list of checks may carry `"from": "lakana.yaml"` on a check that took its
`on_failure` from the project's default, and `"generated"` (`"key"` or `"references"`) on a check
generated from the asset's header.

### When a commit through the Iceberg REST catalog is refused

A caller who may not read the table, or whose load the table would refuse (it is not published,
or kept from a task), gets fixed texts instead of the details, which would name snapshots,
references or properties of the table. A reader gets the details. For such a caller, removing a
property the table does not have changes nothing instead of failing, as in Iceberg itself.

| The message | Cause | What to do |
|---|---|---|
| `The table changed while this write was in progress, so it was not applied. Run the write again; an Iceberg client has to load the table again first, which needs READ on the table.` | A requirement of the commit no longer holds: somebody else changed the table (409). An SDK append or replace that lost a race gets it too. | Run the write again. An Iceberg client loads the table first, which needs `READ_TABLE`. |
| `The updates do not fit the table's current metadata, so nothing was applied. The details need READ on the table.` | An update cannot be applied to the table as it is now, for example a ref set to a snapshot the table does not have (422). | Ask for `READ_TABLE` on the table to see the details, or ask its owner. |
| ``An Iceberg commit cannot remove snapshots or the tag of a failed check: running tasks and kept tags still need them. Expire snapshots with `lakana table maintain`, which keeps what they need.`` | A commit through the Iceberg REST catalog (PyIceberg, another engine, your own client) removes snapshots, for example by expiring them, or removes a `lakana-check-failed-*` tag (403, for every caller). | Expire snapshots with `lakana table maintain`. Removing a branch or a tag of your own is still allowed. |
| ``An Iceberg commit cannot set a ref named `lakana-check-failed-...`: Lakana keeps the snapshot of a failed check under such a name. Use another name for your own ref.`` | A commit sets, moves or creates a ref (a tag or a branch) whose name starts with `lakana-check-failed-` (403, for every caller). | Use another name. |
| `Lakana keeps every schema, partition spec and sort order a table has had, because its snapshots and a set-back may need them. Leave them in place; an unused one costs nothing.` | A commit removes a schema or a partition spec (403, for every caller), one in use or not. | Leave them; a table keeps its earlier schemas and specs. |
| `A table's UUID cannot change.` | A commit assigns a UUID other than the table's (422, for every caller). Every later write and the set-back of a failed check require it. | Leave the UUID alone. |
| ``The commit adds `FILE` to `TABLE`, which has no Iceberg field ids. Lakana reads Iceberg data by field id, as the Iceberg specification requires. Write the file with a client that sets field ids.`` | The commit adds a data file whose columns carry no Iceberg field ids, for example one registered with pyiceberg's `add_files` (422, for every caller). Nothing is applied. | Write the file with a client that sets field ids (pyiceberg's `append` and `overwrite` do) and commit again. |
| ``The commit adds `FILE` to `TABLE`, which could not be read as a Parquet file, so nothing was applied. Write the data file before the commit, as Parquet with Iceberg field ids.`` | A data file of the commit is missing, is not Parquet, or has a footer that does not parse (422). | Write the file before the commit, as Parquet. |
| ``The commit adds `FILE` to `TABLE` as ORC, so nothing was applied. Lakana reads Parquet data files only; write the file as Parquet.`` | A data file of the commit is ORC or Avro (422). | Write it as Parquet. |
| ``A manifest of the commit's snapshot for `TABLE` could not be read, so nothing was applied. Write the manifests and the manifest list before the commit.`` | The manifest list or a manifest of the commit is missing or unreadable (422). | Write them before the commit. |
| ``The commit adds more than 2,000 data files to `TABLE`; Lakana checks at most 2,000 per commit. Split the load into smaller commits.`` | A commit adds more than 2,000 data files (422). Nothing is applied. | Commit the load in batches of at most 2,000 files. |
| ``Checking the data files the commit adds to `TABLE` for Iceberg field ids took longer than 60 seconds, so nothing was applied. Split the commit into smaller ones.`` | Reading the manifests and the footers of the added files together took longer than 60 seconds (422, for every caller). Nothing is applied. | Split the commit into smaller ones: the answer is a client error (422), so a client does not retry it. |
| ``The commit adds `FILE` to `TABLE`, whose Parquet footer is larger than 16 MiB, so nothing was applied. Write the file with fewer columns and row groups.`` | A data file of the commit has a Parquet footer above 16 MiB (422). | Write the file with fewer columns and row groups. |
| ``The commit's snapshot for `TABLE` names `FILE`, a manifest file larger than the 64 MiB Lakana reads, so nothing was applied. Split the commit into smaller ones, so that each manifest lists fewer data files.`` | The manifest list or a manifest of the commit is larger than 64 MiB (422). Lakana reads such a file whole, so it stops reading it at 64 MiB. The limit also applies to the manifest list of the snapshot's parent, which the committing client did not send (a table whose current snapshot has a list that large refuses such commits; this takes tens of thousands of manifests). Nothing is applied. | Split the commit into smaller ones. |
| ``Storage could not be read right now, so the data files the commit adds to `TABLE` were not checked and nothing was applied. Try the commit again.`` | Storage failed while the commit's files were checked (503, with `Retry-After`). | Try the commit again. |
| `Making an earlier schema, partition spec or sort order current needs READ on the table. Add a new one and make that one current, or ask for READ.` | A caller without `READ_TABLE` makes a schema, partition spec or sort order current that the table had before, instead of one the same commit adds (422). | Ask for `READ_TABLE`, or add a new one and make that one current. |
| `This schema change adds a schema under an id the table already uses, so nothing was applied. Load the table again and make the change on what the load gives; loading needs READ on the table.` | A schema change reuses a schema id of the table, which would bring an earlier schema back (422, for every caller). An Iceberg client that sees only the current schema picks such an id after the table went back to an earlier schema. | Load the table again and repeat the change; loading needs `READ_TABLE`. |
| `This partition spec change adds a partition spec under an id the table already uses, so nothing was applied. Load the table again and make the change on what the load gives; loading needs READ on the table.` | A commit adds a partition spec under an id the table has, whether its content is the same or not (422, for every caller). Accepting the same content would tell a caller without `READ_TABLE` that its guess of an earlier spec is right. | Load the table again and repeat the change; loading needs `READ_TABLE`. |
| `This sort order change adds a sort order under an id the table already uses, so nothing was applied. Load the table again and make the change on what the load gives; loading needs READ on the table.` | The same for a sort order (422, for every caller). | Load the table again and repeat the change; loading needs `READ_TABLE`. |

## Who may read the results

- The results of one run need what reading the run needs: `READ_ENVIRONMENT` on the environment,
  or `RUN`.
- Listing the results of an environment needs `READ_ENVIRONMENT`.

Looking at the rows of a check needs `READ_ENVIRONMENT` or `RUN`, and `SUBMIT_QUERY`; see [The
rows of a check](#the-rows-of-a-check).

Running a check needs no permission of its own. It is part of a run and reads as the run's
principal; that principal needs the right to read every table the check names, as for any query.
Setting a table back is not a route and not a command: only the worker that runs the task does it.

## What is audited

| Event | When | Details |
|---|---|---|
| `CHECK_FAILED` | A check that stops the pipeline (`block` or `keep`) failed or errored. One event for each such check. | `run_id`, `task_run_id`, `table`, `check` (the name), `path`, `outcome` (`failed` or `errored`), `rows`. |
| `TABLE_ROLLED_BACK` | After a set-back, whatever it did, and not when its outcome is not known. | `run_id`, `task_run_id`, `table`, `before` and `after` (snapshot ids), `checks` (the failed ones), `outcome` (a rollback outcome above), and `schema_before` and `schema_after` when the table's columns were set back, too. When the set-back moved the table to data that its verdict did not describe: `kept_restored`, the state that the table has again (`run_id`, `task_run_id`, `check`, `unchecked`, and `unknown: true` for a mark of unknown checks), or `null` when it has no mark now; or `kept_unknown: true` when the history had no state for that data and the table got a mark that says its checks are not known. `kept_not_restored: true` marks a first build that kept its mark. |
| `TABLE_KEPT` | A `keep` check failed and the table got its mark. Not when the same task sets the same mark again. | `run_id`, `task_run_id`, `checks` (the failed ones, sorted). |
| `TABLE_KEPT_CLEARED` | The mark was cleared. | `run_id` and `task_run_id` of the clearing run and task, and `why`: `checks_passed` or `rebuilt_without_checks`. |

A passed or warned check is in the results and not in the audit log. Read the events with
`lakana audit list --event-type CHECK_FAILED`.

## What a project stores

A deploy stores each check in the project version's manifest: its name, table, path, `on_failure`,
description and SQL. A manifest with checks has the version number 4, and a project without
checks keeps its old number. A worker of an earlier release refuses a version-4 manifest with the
message that tells you to update the worker; upgrade the workers together with the server (see
[Upgrades](../explanation/upgrades.md)).

A task never builds a table unchecked when its project version lists checks for it. If a run was
started by a server that does not know checks, the task fails before it writes anything, with a
message that says to upgrade the server (see
[Upgrades](../explanation/upgrades.md#data-quality-checks)).

If the stored manifest holds a check entry that cannot be read, which a deploy never writes but a
hand-edited or damaged manifest can, the task fails and the table is set back, or left not
published when this run built it for the first time. An entry is unreadable when a field is
missing or is not text, when its name is not `<table>/<check>`, or when two entries have the same
name (both count). The other checks of the table still run and are recorded, and one more
blocking result, named `<table>/(unreadable)`, says that the project version lists a check that cannot
be read. A deploy of the same commit reuses the stored project version, so it does not repair this:
deploy a new commit of the project, which writes the project version afresh. An entry that does not
name a table of the manifest, a `checks` entry that is not a list, and a version-4 manifest
without any check count against every table of the run, so every task that builds a table fails.
The names `(none)` and `(unreadable)` are reserved; a check file cannot have them.
