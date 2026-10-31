# Stop a query

After this guide you can find your queries, stop one that you started, with Ctrl-C or with a
command, and you know what else ends a query and what cancelling does not reach. You need the
query's id only for the command; Ctrl-C needs nothing.

Every query runs in a process of its own, which the worker starts and can end. A cancel asks the
worker to end that process. A query that ends `CANCELLED` has stored nothing; one that was already
storing its result or committing its write when the cancel arrived is not interrupted and ends
`SUCCEEDED` (see below).

## Find a query

`lakana query list` shows the queued and running queries of an environment: the running ones first,
then the queued ones in the order the workers will take them, with the reason each one waits
(see [Why is my query or task waiting?](why-is-it-waiting.md)).
`--all` adds the queries that ended in the last 24 hours, the newest first, after the others;
`--limit N` (1 to 200, default 100) cuts the list, and a list that was cut ends with the
command for the next page. `--status` and `--submitter` narrow it (see
[Find a run, a query or a deploy](find-a-run.md)). This is the list for a query you
started with `lakana query submit`, or one whose `lakana sql` you closed before it printed an id:

<!-- source: docs/examples/getting-started/run.sh#query-list -->
```bash
lakana query list --env dev --all
```

<!-- output -->
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ id                                   ┃ submitter ┃ status    ┃ waiting_for ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ ee2a567d-a884-4231-a26d-976e2d12b456 │ admin     │ SUCCEEDED │             │
│ 9de2a82e-3dab-4d57-ac4b-eb248bebf45b │ admin     │ SUCCEEDED │             │
│ e4277608-b5f7-41cd-8997-085a40319d3a │ admin     │ SUCCEEDED │             │
│ b4658e0c-78de-4bd8-93c2-4904c76688b9 │ admin     │ SUCCEEDED │             │
└──────────────────────────────────────┴───────────┴───────────┴─────────────┘
Not shown: age, kind. Use --output json to see them.
```

The table has `id`, `submitter`, `kind` (`read`, `create TABLE`, `replace TABLE`, `append to
TABLE`, `view TABLE` or `maintenance of TABLE`), `status`, `age` (the time since the query was
submitted, as `12s`, `3m`, `2h` or `1d`) and `waiting_for` (why a queued query has not started;
empty for any other status). On a narrow terminal the columns `age`, `kind` and `submitter` are
left out one by one and the line under the table says which; `--output json` has every field.
A line `No queued or running queries in ENV.` means there is nothing to find, and `--all` then
looks 24 hours back.

When the limit of running ad-hoc queries per user is reached, a further query of yours waits with
the reason `user_limit`, and stopping one of your own running queries lets it start (see
[Why is my query or task waiting?](why-is-it-waiting.md#a-queued-query)). A query that waits for
the limit is passed by: younger queries of other people start before it, so it can stand above
them in this list.

You see the queries you submitted. A holder of `MANAGE_ENVIRONMENT` on the environment (the `ADMIN`
role includes it) sees everyone's, and can cancel them. The list never shows a query's SQL, its
error message or its result, not even your own: read those with `lakana query status ID`, which
only the person who submitted the query can do. A task's token lists only the queries it
submitted. From Python, `client.queries.list("dev")` returns the same rows.

## Cancel a query

Press Ctrl-C while `lakana sql` or `lakana query wait` waits. A query that is queued for more than
3 seconds also prints, on standard error, why it has not started
([Why is my query or task waiting?](why-is-it-waiting.md)); Ctrl-C cancels it all the same. The command sends a cancel for its
query, prints what happened on standard error and exits with status 130, the status a shell reports
for Ctrl-C. `lakana query wait` cancels the query it waits for in the same way, also when it is
someone else's query and you hold `MANAGE_ENVIRONMENT`; anyone else is told that the query was not
cancelled:

<!-- output -->
```text
Interrupted. Cancelling query 7b0c4f7a-2f0e-4c1b-9d3a-5a1e6c0f9a11; press Ctrl-C again to stop waiting for it.
Interrupted. Cancelled query 7b0c4f7a-2f0e-4c1b-9d3a-5a1e6c0f9a11.
```

To cancel a query from another terminal, or one you started with `lakana query submit`, give its
id:

<!-- not tested: needs the id of a running query -->
```bash
lakana query cancel QUERY_ID --env dev
```

Whoever submitted a query may cancel it, and so may anyone who holds `MANAGE_ENVIRONMENT` on its
environment (the `ADMIN` role includes it). A task's token cancels only the queries it submitted.
Someone who may not cancel a query gets the answer for a query that does not exist, so a cancel
tells you nothing about other people's queries.

The command prints one of three answers, by the state of the query when the server got the request:

| The command prints | What it means |
|---|---|
| `Cancelled query ID.` | The query had not started, or it had started and its worker is gone (see [A worker that is lost](#a-worker-that-is-lost)): it is `CANCELLED` now and will not run or go on. This holds for a maintenance run that had started and whose worker is gone, too. |
| `Asked query ID to stop. A running query ends within the grace period (10 s unless the operator changed it); check with 'lakana query status ID --env dev'.` | The query is running. The worker looks for a cancel request about once a second, asks the query's process to stop, and ends the process if it has not stopped after the grace period (`LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS`). The status stays `RUNNING` until then. A query that finishes within the second or so before the worker notices the request also ends `SUCCEEDED`. A query that is already storing its result or committing a write when the request arrives is not interrupted: it ends `SUCCEEDED`, and for a write the data is there. During a write, the worker sees the request when the batch in hand has been stored. |
| `Query ID had already ended (SUCCEEDED); nothing was changed.` | The query finished first. A cancel of a finished query is not an error and changes nothing; the status is the one it ended with. |

A fourth answer is a refusal, not a result. While a worker is starting a query or finishing it
(storing the result, committing the write), the query's row in the database is locked, and a
cancel waits for it for 2 seconds on PostgreSQL. If the row is still locked, nothing is changed and
the command exits with status 7 and prints `Query ID is being started or is finishing right now,
so it cannot be cancelled at this moment. Check it with: lakana query status ID --env dev. If it is
still running, cancel it with: lakana query cancel ID --env dev`. Do that: the query has usually
moved on by then, and a write that finished has its data in place. With Ctrl-C the line reads `Interrupted. Query ID was not cancelled: it is being started or is
finishing. Check it with 'lakana query status ID --env dev'; if it is still running, cancel it with
'lakana query cancel ID --env dev'.` From Python the call raises `Conflict`.

A query that ends `CANCELLED` has the cause `cancelled`, stores no result, and commits no write: a
table you wrote into with `--into` keeps the snapshot it had before. To see which of the two
happened to a query that you asked to stop, run `lakana query status ID --env dev`: `CANCELLED`
means nothing was stored, `SUCCEEDED` means the cancel came too late and the result (or the write)
is there. Each
cancel request that the server accepts is audited as `QUERY_CANCELLED`; a request for a query that has
ended is not. A request you may not make (the query is somebody else's, or does not exist) is
recorded as a refused action and answered with the same "not found".

If you cancel with Ctrl-C and the server cannot be reached, the command exits with status 130 all
the same and names the query and the command to run later:

<!-- output -->
```text
Interrupted. Cancelling query 7b0c4f7a-2f0e-4c1b-9d3a-5a1e6c0f9a11; press Ctrl-C again to stop waiting for it.
Interrupted. The query could not be cancelled (Could not reach http://127.0.0.1:8765: ConnectError) and may still be running; cancel it with 'lakana query cancel 7b0c4f7a-2f0e-4c1b-9d3a-5a1e6c0f9a11 --env dev'.
```

A second Ctrl-C while the cancel is on its way ends the command at once, says that the query may
still be running, and names the same command. If you press Ctrl-C before the server has confirmed
the query, there is no id yet and nothing to cancel; the command says so. If the request had
already reached the server, the query may still be queued or running; the message names
`lakana query list --env ENV` to find it (add `--all` for one that just ended) and
`lakana query cancel ID --env ENV` to cancel it. If you press Ctrl-C while the
result of a query that has succeeded is fetched, the command exits with 130 and names `lakana query
result`.

Anyone who waits for a query that is cancelled, with `lakana sql` or `lakana query wait` in another
terminal, gets the same ending as for a query that failed: the message `The query was cancelled.`
and exit status 9. `lakana query status` shows the status `CANCELLED` and the same message.

From Python, a handle cancels its query, and `client.queries.cancel` does the same by id:

```python
status = client.queries.get(query_id, "dev").cancel()
print(status.status)  # CANCELLED, RUNNING, or the status a finished query ended with
```

`QueryRun.wait()` raises `QueryFailed` for a cancelled query, with `cause == "cancelled"`. The SDK
does not cancel when you interrupt it: `client.sql` leaves the query running unless your code calls
`cancel()`.

## What else ends a query

| What ends it | Status and cause | What the message says |
|---|---|---|
| The time limit (`LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS`, 900 s) | `FAILED`, `timeout` | `Query exceeded the time limit (900 s). Aggregate or filter earlier, add a LIMIT, or ask the operator to raise LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS.` A write says that storing the result counts toward the limit instead. The worker asks the query to stop first. If the query has not stopped after `LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS` (10 s), its process is ended and the message adds: `The query did not stop when asked and was ended after the grace period (10 s, LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS).` |
| The time limit of a maintenance run (`LAKANA_RUN__MAINTENANCE_TIMEOUT`, 1800 s) | `FAILED`, `timeout` | `Maintenance ran past its time limit (1800 s) and stopped at a safe point; the table is consistent. Run maintenance on this table again to continue where it stopped, or ask the operator to raise LAKANA_RUN__MAINTENANCE_TIMEOUT.` See [A maintenance run past its time limit](#a-maintenance-run-past-its-time-limit). |
| The worker's memory limit | `FAILED`, `out_of_memory` | `Query failed: this query made the worker exceed its memory limit (3686 MB) and was stopped.` and what to do: read or compute less, or ask the operator to raise the limit. The largest consumer is ended first: the worker compares every running query and every running Python task, so a large task can be ended instead of a query. See [Memory limits](deploy-with-compose.md#memory-limits). |
| A worker that shuts down | `FAILED`, `cancelled` | `The query was stopped because the worker is shutting down. Run it again.` The worker gives a running query the drain grace first (`LAKANA_RUN__DRAIN_GRACE_SECONDS`, 20 s). Run the query again. |
| A query process that ended unexpectedly | `FAILED`, `internal` | `Query failed: the query process ended without a complete result (exit status N). Nothing was stored. Run the query again; if it fails the same way, tell the operator: the worker's log has the details.` Or, when a signal from outside the worker ended it: `Query failed: the query process was ended from outside the worker (signal N). Nothing was stored.` followed by the advice to read or compute less, give the worker more memory, or run the query again. |
| DuckDB loses the error of a statement that failed (rare, with several threads) | `FAILED`, `invalid_query` | `The statement failed, but DuckDB reported an interruption instead of the cause. Run it again to see the error.` Nobody asked the query to stop; DuckDB cancelled one of its own parallel tasks when another failed, and reported that instead of the failure. Run the statement again: it fails with its own message. A task that runs it is not retried, as for any query that fails on its own SQL. |
| A worker that cannot reach the database | `FAILED`, `worker_lost` | `The worker running this query lost its connection to the database and stopped the query. Run it again.` Each worker writes a heartbeat to the database every `LAKANA_RUN__RENEW_SECONDS` (20 s). A worker whose heartbeat has failed for `LAKANA_RUN__LEASE_SECONDS` (60 s) takes no new work and stops the query it runs, then records the failure if the database is back. `worker_lost` is also the cause when the platform ends a run whose worker is gone. The SQL query of a pipeline task is tried again like any other transient failure. Run an ad-hoc query again. |
| A worker that is lost | `FAILED`, `worker_lost` | One of three messages, by what the query was; see [A worker that is lost](#a-worker-that-is-lost). A write that had already been committed ends `SUCCEEDED` instead (its task, if the table has [checks](check-your-data.md), is taken again and only checks; see [A cancel while the checks run](#a-cancel-while-the-checks-run)). |

A query that ended for one of these reasons (except a committed write of a lost worker, whose task is taken again to run its [checks](check-your-data.md) if the table has any) stores
nothing and commits nothing, like a cancelled one. The difference is the status: only a cancel request gives `CANCELLED`; the others give
`FAILED`. When two reasons meet, the first one is recorded: a query that the worker's memory limit
ends while it is still within the grace period of its time limit or of a cancel request is reported
under that first reason, with the sentence that it was ended after the grace period.

### Every cause a failed query can have

`lakana query status ID --env dev --output json` and the SDK (`status.error_cause`,
`QueryFailed.cause`) give the cause as one word. These are all of them:

| Cause | The query ended because |
|---|---|
| `invalid_query` | The SQL cannot be run: a syntax error, an unknown column or function, a type that does not fit. Change the SQL. |
| `not_found` | A table, namespace or environment it names does not exist, or you may not see it. |
| `permission_denied` | You may not do what it asks, for example write to that table. |
| `result_too_large` | The result is larger than the limit. Filter or aggregate earlier, or add a `LIMIT`. |
| `timeout` | It ran longer than the time limit, or, for a maintenance run, longer than `LAKANA_RUN__MAINTENANCE_TIMEOUT`. |
| `out_of_memory` | The worker's memory limit was reached. |
| `cancelled` | A cancel request, or a worker that shut down, stopped it. |
| `worker_lost` | The worker that ran it was lost or lost its connection to the database. Run it again. |
| `internal` | Anything else, such as a query process that ended without a result. The message says what to do. |

## A maintenance run past its time limit

A maintenance run (`lakana table maintain`) ends by itself after `LAKANA_RUN__MAINTENANCE_TIMEOUT`
(30 minutes by default). The worker looks at the clock before it expires snapshots, before it looks
for orphaned files and before it deletes each file, and stops at the first of these points after the
limit. The run is `FAILED` with the cause `timeout`.

- **The table is consistent.** Expiring snapshots is one commit, and files are deleted one by one,
  so the table never sits half-changed. What was expired or deleted stays so; `lakana query status
  ID --env dev` still shows the report of that part.
- **Run maintenance on the table again.** The next run continues with the snapshots and files that
  are left and finishes if it has the time. A very large table may need a few runs; ask the operator
  to raise `LAKANA_RUN__MAINTENANCE_TIMEOUT` if one run is never enough.

## A worker that is lost

A query runs only while a worker looks after it. If the worker is killed, loses power or does not
manage to write its heartbeat for `LAKANA_RUN__LEASE_SECONDS` (60 s), the platform ends the
queries it held, within about a minute and a scheduler step with the defaults. A worker writes
its heartbeat every `LAKANA_RUN__RENEW_SECONDS` (20 s), so a database that is away for the lease
minus that interval (40 s with the defaults) can be enough. A query no longer stays `RUNNING` for
ever: a worker that is alive but could not write the final status of a query or a deploy (the
database was away) ends that run itself, right after the next query, deploy or task it finishes and at
least every few seconds while it is idle, with the same message. "About a minute" holds when another worker can take over the scheduler's role. PostgreSQL is
asked to probe every client and to drop a connection whose machine no longer answers within about
two thirds of a lease, so the role of a worker that lost power or its network is expected to be
freed within about a lease. If
PostgreSQL refuses the request, a warning is logged once at the start and the role of such a worker
can stay with it for hours, with nothing ending lost queries or reclaiming tasks meanwhile. `lakana scheduler status` shows which worker holds the role and when it last reported; if that worker is gone and the role stays with it for much longer than a minute, ending its database session in PostgreSQL (`pg_terminate_backend`), or restarting PostgreSQL, frees it; restarting the other workers does not.
What you read depends on what the query was:

| The query was | It ends | The message |
|---|---|---|
| A read, including the input read of a Python task (see below) | `FAILED`, `worker_lost` | `The worker running this query was lost, so the query was ended. Run it again.` |
| A write (`--into`, an append, a view) that had not been committed | `FAILED`, `worker_lost` | `The worker running this write was lost before it was committed, so the write was ended. Nothing was written to TARGET. Run it again.` |
| Maintenance (compaction, expiry) | `FAILED`, `worker_lost` | `The worker running this maintenance was lost. Part of it may be done; running it again is safe.` |
| A write that had already been committed | `SUCCEEDED` | none: the data is in the table, so running the query again would write it twice |

A write that ends `SUCCEEDED` this way shows fewer details than one the worker finished itself: it
keeps its row count, but has no watermark, no schema difference and no lineage summary on the
run. The table itself is complete. A cancel of a query whose worker is gone is answered at
once (`Cancelled query ID.`), and a cancel of a write that had already been committed answers with
the status `SUCCEEDED`, not `CANCELLED`, because the data is there.

A worker that the database counts as lost may still be running. That happens when a worker was
frozen or its machine slept for longer than the lease: the platform ends what it held, and the
worker, when it wakes, finds its queries ended and stores nothing more; a write it still tries to
commit is refused. The scheduler gives the workers time to beat first: after it was away for more
than half the lease (a pause, a stalled database, a new leader) it ends nothing until
`LAKANA_RUN__RENEW_SECONDS` plus 5 s have passed. If you run Lakana on a laptop (local mode), the
scheduler and the worker are one process: after the machine slept, a running query is ended only
if its worker does not write a heartbeat within about half a minute after waking; then run it
again.

The input read of a Python task gets the message above, with the cause `worker_lost`, when the
platform ends it because its worker is gone. When the task's own lease runs out first, the task
is retried and its read carries the task's lease message and no cause; either way the task sends
the read again.

A pipeline's SQL task is different: its own write belongs to the task. While the task's lease is
live the platform does not end the write because of a heartbeat. Once the lease is lost, what
happens depends on the write. A write that had not been committed ends `FAILED` and the task is
tried again. A write that had been committed ends `SUCCEEDED`, and so does the task: it is **not**
run again, so an `append` cannot land twice, and the tasks below it go on. (A task whose table has
[checks](check-your-data.md) is the exception: it does not end `succeeded` before its checks have
run. It is claimed again, builds nothing, and runs only its checks; see
[A cancel while the checks run](#a-cancel-while-the-checks-run).) This also holds when the
worker is alive but recording the task's outcome failed after the commit (the task may then carry
a message that says recording its details failed), when the worker is asked to drain, and when the
run is being cancelled at that moment (the data is there, so the task is `succeeded`, not
`cancelled`) and when the worker was stopped for its memory limit after the commit. A task whose worker was
lost shows `The worker was lost after this task's write had been committed. The task counts as
succeeded and was not run again.` as its message (`lakana runs show` prints it in the task's
last column as `note: …`, not counted among the errors, and `lakana runs logs` shows it on the attempt), with the rows the write stored. It has
no watermark and no schema change: the worker was gone before it recorded them. Its finish time
is the moment the platform found the commit, so the duration shown includes the wait for the
lease to run out.

When a worker that was counted as lost wakes up and tries to commit a write of a run that was
already ended, the platform refuses the commit and nothing is written. The task's attempt then
shows `This query run was ended because its worker was lost; nothing was written. Run it again.`,
and the task is tried again.

### A cancel while the checks run

A task whose table has [checks](check-your-data.md) has two phases: it writes the table, and then
counts each check with a query of its own. `lakana runs cancel RUN_ID --env dev` stops the count
that is running. That query ends `FAILED` with the cause `cancelled`, not `CANCELLED`, and the task
ends `cancelled`. **The table is set back first:** the table holds what the run wrote, and it has
not been checked, so the worker hands the task back and the next free worker sets the table back
to before the task ran, and only then does the task end `cancelled` (see [A worker that is
lost](check-your-data.md#a-worker-that-is-lost)). Run the project again. A later `lakana runs
resume` of an incremental asset does not write the increment twice.

A worker that is lost while a task counts its checks does not end the task `succeeded`, because the
table is written but not checked. The platform ends the count with `The worker counting this check
of a pipeline task was lost, so the count was ended. The check is counted again when the task is
taken again.` and another worker takes the task, skips the write and counts the checks. This
repeats each time a worker is lost during the checks, once per lease, until the checks finish, you
cancel the run, or 5 more tries have been lost: then the table is set back and the task fails with
the code `checks_unfinished` (see [A run that does not
end](check-your-data.md#a-run-that-does-not-end)). The task shows the lost attempts.

### A write whose commit was not confirmed

A write can also be committed although the answer never reached the worker, for example when the
connection to the database dropped just after the commit. The platform then reads the table
again before it deletes anything. If the write is there, nothing is deleted and the write is
reported as done. If it is not, the files it wrote are deleted, as for any failed write. If the
table cannot be read either, or a commit is still being applied after a few seconds, the files
are kept and the write fails with `The write to TABLE may or may not have been committed: …`. In
that case do not repeat the write yet: read the table (`lakana sql "SELECT count(*) FROM
TABLE"`) or look at the query (`lakana query status ID`; a query whose write was committed shows
`SUCCEEDED`), and only repeat the write if its rows are not there, since an `append` that did
land would otherwise land twice. (The query shows `SUCCEEDED` in that case. The task of a table with [checks](check-your-data.md) is not ended `succeeded` before its checks ran.) Files that belong to a write into an existing table that did
not commit stay as unreferenced files until `lakana table maintain` removes them. A table that
was being created needs no command: if it did not appear, the worker's sweep removes its files once
it has proven that the commit did not land (after the run has ended, or 24 hours for a create
outside a run; see [The sweep](deploy-with-compose.md#the-sweep)), and creating it again is safe.
A pipeline task that fails this way is not retried by itself.

## What cancel does not do

- **The write of a pipeline task, and the count of a check.** The query behind a task of a run
  belongs to the run. A cancel is refused with `Query run ID is the write of a task in pipeline run
  RUN, not a query of its own. Cancel that run instead: lakana runs cancel RUN --env dev`, and the
  CLI exits with status 8. A check is counted by a query of its own, which appears in `lakana query
  list` while it runs, and its refusal reads `Query run ID counts a check of a task in pipeline run
  RUN; it is not a query of its own.` followed by the same advice. Cancel the run; see [Projects
  and deploys](projects.md).
- **A maintenance run that has started.** A queued one is cancelled; one that has started cannot be
  stopped while its worker is alive (once its worker is gone, a cancel ends it at once); it ends by
  itself at the first safe point after `LAKANA_RUN__MAINTENANCE_TIMEOUT`, 30 minutes by default, as described under
  [A maintenance run past its time limit](#a-maintenance-run-past-its-time-limit). The refusal says `Query run ID is a maintenance run that has already started. It cannot be
  stopped and ends by itself; follow it with: lakana query status ID --env dev`.
- **The input query of a Python task.** A query that a Python task sends to load its inputs is an
  ordinary query, and you can cancel it. The task's attempt then fails as a failure it may retry
  and sends the query again. Cancel the run, not the query; see [Projects and
  deploys](projects.md).
- **macOS and Windows.** On macOS a query's process is not ended with a worker that is killed, and
  Windows has no process groups. Python tasks have the same limits.
