# Why is my query or task waiting?

When a query seems stuck, read the line that starts with `Waiting:` on standard error, or run
`lakana query status QUERY_ID --env dev`; for a deploy, read the same line from `lakana deploy`
or run `lakana deploys show DEPLOY_ID --env dev`; for a task of a pipeline, run
`lakana runs show RUN_ID --env dev`. The sentence names the reason, and the tables below say what
each one means and what to do. Most of them need nothing from you.

After this guide you can read the reason Lakana gives for a query, a deploy or a task that has not started,
you know which reasons need you to do something and which ones pass by themselves, and you know
what to look at when a reason does not go away.

A query waits in the state `QUEUED` until a worker takes it, and a deploy in the state `queued`. A task of a run waits in `pending` or
`ready`. For as long as it waits, the platform says why, in one sentence. The sentence is worked
out when you ask; it is not stored, and the numbers in it are a good estimate, not a promise (see
[The numbers are estimates](#the-numbers-are-estimates)).

A task that failed instead has a failure code and a next step: see
[Why did it fail?](../reference/why-did-it-fail.md).

## Where you see the reason

| You run | The reason appears as |
|---|---|
| `lakana sql`, `lakana query wait` | One line on standard error, `Waiting: <the sentence>`, once the query has been queued for 3 seconds, and again each time the reason changes. A query that starts sooner prints nothing. Standard output carries only the result, and with `--output json` nothing about waiting is printed. |
| `lakana deploy`, `lakana rollback` | The same line on standard error, `Waiting: <the sentence>`, once the deploy has been queued for 3 seconds, and again each time the reason changes. Standard output carries only the result, and with `--output json` nothing about waiting is printed. |
| `lakana deploys show ID` | The `waiting_for` object of a queued deploy, with `--output json`; `null` once it runs. |
| `lakana query status ID` | The `waiting_for` row; empty for a query that is not queued. `--output json` has the whole object. |
| `lakana query list` | The `waiting_for` column of each queued query (see [Stop a query](stop-a-query.md#find-a-query)). |
| `lakana runs show RUN_ID` | The `waiting_for` column of the task table; empty for a task that is not waiting. |
| `lakana job list` | The state of each job, and under the table the sentence of each job that waits or cannot start (see [A waiting job](#a-waiting-job)). |
| `lakana run` | The sentence at the end of a task's progress line, and a line of its own when a task starts to wait for `no_worker`, `no_worker_of_kind`, `workers_busy` or `retry`. |
| The SDK and the REST API | `status.waiting_for` and the task's `waiting_for`: an object with a `code` and a `message` (and numbers such as `workers`, `ahead`, `running` and `limit`, only the ones the code uses), `None` or `null` when nothing is waited for. `QueryRun.wait(on_update=f)` and `client.deploys.wait(env, id, on_update=f)` call `f` with every status they poll. A deploy has `waiting_for` on the single read only, not in the history. |

A query that waited behind a busy worker for ten seconds prints this on standard error and then
runs; its result goes to standard output as always:

<!-- output -->
```text
Waiting: All 2 workers are busy, and about 4 queries or deploys are ahead of this one.
```

The line is printed again only when the reason itself changes (from `workers_busy` to
`no_worker`, say), never because a number in it changed. The three seconds count from the first
time the command saw the query queued.

To read a query's reason yourself, give its id:

<!-- not tested: needs the id of a queued query -->
```bash
lakana query status QUERY_ID --env dev
```

## A queued query

The rows are in the order the reasons are tested: the first one that holds is the one you see.
`user_limit` is tested only while a worker that takes the query's kind is free; with every such
worker busy, the reason is `workers_busy` whether or not you are at the limit. `lock_timeout` comes
next and is also said only while such a worker is free.
A query is either *interactive work* (an ad-hoc query you or a tool submit) or *pipeline work*
(see [Which worker takes which work](#which-worker-takes-which-work)); a reason that says "this
kind" means the kind of the query that waits.

| Code | The sentence | What to do |
|---|---|---|
| `no_worker` | `No worker is running, so this query cannot start. Ask the operator to start a worker; on a local instance, start it again with 'lakana server start --local'.` | No worker is registered, or none has sent a heartbeat for `LAKANA_RUN__LEASE_SECONDS` (60 s). Start one: on a Compose instance `docker compose up -d` (it starts the workers your `.env` asks for); on a local instance the worker belongs to the command that runs the instance, so see [Local instance](#local-instance-and-compose-instance). In three cases a worker is there and waiting a moment is right; see [When "no worker" passes by itself](#when-no-worker-passes-by-itself). |
| `no_worker_of_kind` | `No worker that takes interactive work is running (2 workers take pipeline work only), so this query cannot start. Ask the operator to start a worker with LAKANA_WORKER__TAKES=interactive or all.` Also `1 worker takes`, `pipeline` and `interactive` swapped for a pipeline query, and `this maintenance run` for table maintenance. | Workers are running, but none of them takes this kind of work, which is a matter of settings and does not pass until a worker that takes it starts. Tell the operator: `lakana worker list` shows what each worker takes in its `takes` column. An operator starts a worker with `LAKANA_WORKER__TAKES=interactive` (or `pipeline` for pipeline work, or `all`), see [Seeing that a kind has no worker](deploy-with-compose.md#seeing-that-a-kind-has-no-worker). |
| `user_limit` | `You have 2 queries running, which is the limit per user here; this one can start when one of them ends. 'lakana query list' shows them.` With one query: `You have 1 query running, which is the limit per user here; this one can start when it ends. 'lakana query list' shows it.` Above the limit (the limit was lowered, or a worker died): `You have 3 queries running and the limit per user here is 2; this one can start when fewer than 2 are running. 'lakana query list' shows them.` or, for a limit of 1, `You have 2 queries running and the limit per user here is 1; this one can start when none is running. 'lakana query list' shows them.` | Nothing is wrong. A worker is free, but you, and anyone who uses your account, already run as many ad-hoc queries as the limit allows, so that other people's queries can use the free worker. The count is over every environment, while `lakana query list --env ENV` shows one: look in each environment you use. Wait, or free a place by cancelling one of your running queries with `lakana query cancel ID --env ENV` (see [Stop a query](stop-a-query.md)). An operator can raise `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` (see [Settings](deploy-with-compose.md#settings)); that helps only here, while a worker is free. When every worker that takes ad-hoc work is busy you see `workers_busy` instead, even if you are at the limit: the step then is to wait or to add a worker. |
| `lock_timeout` | `A worker waited too long for the lock that serialises the start of this user's queries, so workers pass them by until 12:00:30 UTC. This query starts then, at the latest; nothing needs doing.` | Nothing. Two of your queries were being started at the same moment, and a worker gave up waiting for the one that held the start lock. Workers then leave your ad-hoc queries alone for 30 seconds, so that they do not wait again; this query starts when that time is up. It is rare, and the reason ends by itself. |
| `starting` | `A worker is free; the query starts within about a second.` With queries ahead: `A worker is free; about 3 queries or deploys are ahead of this one, and it starts as soon as they are taken.` | Nothing. A worker looks for work about once a second. |
| `workers_busy` | `All 2 workers are busy, and about 4 queries or deploys are ahead of this one.` Also `The only worker is busy, ...`, `... nothing is ahead of this one` and `... about 1 query or deploy is ahead of this one`. When another running worker does not take this kind of work, the sentence says which workers are meant: `All 2 workers that take interactive work are busy, ...` or `The only worker that takes pipeline work is busy, ...`. | Nothing is wrong. Every worker that takes this kind of work runs something and the query waits its turn. Wait, or ask the operator for more workers (see [Scaling workers](deploy-with-compose.md#scaling-workers)). A long query that holds a worker is stopped with [`lakana query cancel`](stop-a-query.md). |

A manager of the environment (a holder of `MANAGE_ENVIRONMENT`) who lists another person's queued
query sees `user_limit` in a sentence of its own, `Its submitter has reached the limit of 2
running queries per user; it can start when fewer than 2 of theirs are running.` (`... the limit
of 1 running query per user; it can start when none of theirs is running.` for a limit of 1). It
says that their limit is reached and never how many queries they run.

Two things to know when you read `lakana query list`. The `workers` number is the number of running
workers that take this kind of work, and `ahead` counts the queued work that those workers take
before this query: the same kind of work, and the other kind too while a worker takes both. And a
query that waits for the limit is passed by, so queries submitted after it, by other people, start
before it: it can stand above them in the list and still start later.

## A queued deploy

A deploy, a dry run and a rollback are pipeline work and are never held back by the limit per
user, so a deploy says one of four reasons: `no_worker`, `no_worker_of_kind`, `workers_busy` or
`starting`. They mean what they mean for a query, with `deploy` in the sentence (`No worker is
running, so this deploy cannot start. ...`, `A worker is free; the deploy starts within about a
second.`), and the `ahead` count includes the queries and deploys of every environment that are
queued before it. `no_worker_of_kind` is the one that needs the operator: no running worker takes
pipeline work, so the sentence ends `Ask the operator to start a worker with
LAKANA_WORKER__TAKES=pipeline or all.` `lakana worker list` shows the `takes` column.

## A waiting task

A task of a running run shows its reason. A ready task that is setting its table back shows
`setting_back`, also in a run that you cancelled, unless no worker could take it: `no_worker` and
`no_worker_of_kind` come first, because starting a worker is what helps then.

| Code | The sentence | What to do |
|---|---|---|
| `upstream` | `Waiting for upstream tasks: a, b.` At most five names, then `and 3 more`. An upstream table you may not read is never named, only counted: `Waiting for upstream tasks: a and 1 table you may not read.` (`hidden_upstream` in the JSON). A table that does not exist reads the same. | Nothing: the task starts when those tasks have succeeded. Follow them with `lakana runs show RUN_ID --env ENV`. A task behind a failed upstream task shows no reason, and becomes `upstream_failed`. To see the hidden names, ask an administrator for READ on those tables. |
| `retry` | `Attempt 1 of 3 failed; the next one starts at 12:00:30 UTC.` | The task failed and tries again at that time. To see why it failed, read its failure code with `lakana runs show RUN_ID --env ENV` ([Why did it fail?](../reference/why-did-it-fail.md)) and its log with `lakana runs logs RUN_ID --task ASSET --env ENV`. If it fails every time, fix the cause and deploy again. |
| `retry` | `The table is built; its checks could not finish and run again at 12:00:30 UTC.` | The run wrote this table, but the task could not finish checking it (a worker was lost, or a check hit a transient error). It runs its checks again at that time; the table is not built again. If it never gets through, read the task's log; `lakana runs cancel RUN_ID --env ENV` ends it, after the next free worker has set the table back (the reason is then `setting_back`). |
| `no_worker` | `No worker is running, so this task cannot start. Ask the operator to start a worker; on a local instance, start it again with 'lakana server start --local'.` | As for a query. |
| `no_worker_of_kind` | `No worker that takes pipeline work is running (2 workers take interactive work only), so this task cannot start. Ask the operator to start a worker with LAKANA_WORKER__TAKES=pipeline or all.` | Workers are running, but none of them takes pipeline work, and a task is pipeline work. Tell the operator, as for a query: they start a worker with `LAKANA_WORKER__TAKES=pipeline` or `all`. |
| `setting_back` | `This task's table may hold rows no check has passed; the next free worker sets the table back.` | The task wrote, or may have written, a table whose checks have not passed, and either its worker was lost on the task's last attempt, its worker stopped itself for memory on that attempt, or you cancelled the run (a worker that sees the cancel after the write hands the task back too). A cancel does this also for a task that was already waiting: for its checks after a lost worker, or for the next attempt of a Python asset with checks. If no worker is running, the reason is `no_worker` or `no_worker_of_kind` until one is. The next free worker only sets the table back, then ends the task `failed` (or `cancelled` for a cancelled run), with a message that says what became of the table: see [A worker that is lost](check-your-data.md#a-worker-that-is-lost). It is shown also in a run you cancelled. Nothing to do; if no worker runs, start one. |
| `workers_busy` | `Ready, and all 2 workers are busy. Queued queries and deploys are served first.` When another running worker does not take pipeline work: `Ready, and all 2 workers that take pipeline work are busy. ...` or `Ready, and the only worker that takes pipeline work is busy. ...` | Wait, or ask the operator for more workers. Workers take queued queries and deploys before tasks, so a task waits longer than its position suggests while queries are queued. |
| `starting` | `Starts within about a second.` | Nothing. |

A task of a table with [checks](check-your-data.md) has a second phase after its write. During it
the task is `running`, not waiting, and the queries that count its checks appear in `lakana query
list` while they run. A task that only checks a table the project does not build has no write; it
waits for a worker that takes pipeline work like any task, and the assets that read the table wait
for it, with the reason `upstream`.

The rows are in the order they are tested. A task is pipeline work and is never held back by the
limit per user, so it never says `user_limit`. A task has a reason only while its run is running; a run that is only queued, or one that was
cancelled, shows none. In `lakana run`, a task that waits to try again prints the `retry` sentence
instead of `retrying after attempt n/max`, because it says the same and the time as well.

## A waiting job

A job is a named selection of a project that starts by hand, through the API or on a schedule. Only
one run of a project is active at a time, so a start of a job that meets an active run does not fail:
it waits, and begins when the project is free. `lakana job list` shows the state of every job, and
under the table the sentence of each job that has a reason.

| Code | The sentence | What to do |
|---|---|---|
| `job_waits` | `A start of this job waits for run 3f2c... of this project to finish; it starts then.` The state of the job is `waiting`. `lakana job run` prints `` `daily` waits for run 3f2c... to finish; it starts then. `` and exits 0. | Nothing. Only one start waits per job: another start of the same job, or a schedule that fires again, merges into it, so a job never piles up starts. A paused job keeps its waiting start and still starts it. If you start a job by hand while a scheduled start of it waits, your request merges into the scheduled one: the run that begins is the scheduled run (it writes as the bound service account and its trigger is `schedule`), and your request stays on record in the audit event `JOB_START_WAITING` and in the count `merged`. A start that waits is checked again when it begins: if the person who asked was deactivated or may no longer `RUN` in the environment, or no run principal can be chosen any more, the start is dropped and the audit event `JOB_START_DROPPED` says why; `lakana job list` does not show the reason, so ask an administrator who can read the audit log (`lakana audit list`). Follow the run it waits for with `lakana runs show RUN_ID --env ENV`; to start something else first, cancel that run with `lakana runs cancel RUN_ID --env ENV`. |
| `no_run_principal` | `This job has a schedule but no run principal is bound, so it does not run by itself; an admin can bind one with `lakana deployments set-run-as`.` The state of the job is `no run principal`. | A schedule never runs as a person, only as the service account bound to the deployment. Ask an administrator to bind one: `lakana deployments set-run-as REPO --env ENV --principal NAME`. Times that pass until then are not caught up. Starting the job by hand follows the rules of `lakana run`. |
| `run_principal_unusable` | `This job has a schedule, but the run principal bound to the deployment is inactive or is not a service account, so it does not run by itself; an admin can bind another with `lakana deployments set-run-as`.` The state of the job is `run principal unusable`. | The bound account was deactivated or deleted, or is not a service account. Ask an administrator to bind a working one: `lakana deployments set-run-as REPO --env ENV --principal NAME`. Times that pass until then are consumed, not caught up, and the audit event `JOB_START_DROPPED` records each. |
| `job_starts_soon` | `A start of this job is waiting and begins within a few seconds, when the scheduler next looks.` The state of the job is `waiting to start`. | Nothing: the project is free and the scheduler has not looked yet. If it stays so, no worker runs the scheduler: see `no_worker` above. |

The other states need nothing: `scheduled` (it fires at the next run shown), `paused` (see
`lakana job resume`) and `no schedule` (it starts only by hand or through the API). A job that is
both paused and waiting shows `waiting`; the `paused` field of `--output json` says it is paused.

## When "no worker" passes by itself

`no_worker` says to start a worker. Check first whether one is there, because in three cases a
worker exists or is about to, and the right step is to wait a moment:

- **A worker was just started.** Its first heartbeat takes a few seconds. A query submitted in
  that time says `no_worker` and starts when the heartbeat arrives.
- **A supervisor is restarting a worker** (a Compose instance). A worker that exceeded its memory
  limit stops itself (see [Memory limits](deploy-with-compose.md#memory-limits)), and the container
  manager starts it again. In the gap, and for the seconds after the restart, there is no worker.
- **A local instance after the machine slept.** The worker is still there, but its heartbeat is
  older than the lease, so it counts as not running until the next beat, which comes within about
  20 seconds after the machine wakes.

If the reason is still `no_worker` after a minute, start a worker as the next section says.

## Local instance and Compose instance

The server's sentence says `Ask the operator to start a worker; on a local instance, start it
again with 'lakana server start --local'`. It is the same sentence for every kind of instance,
so here is what it means for each:

- **A local instance.** `lakana server start --local` runs the server and the worker together in
  one command, and the command ends when either of them ends. If that command is still running in
  its terminal, a worker is there: wait a moment (the cases above). Starting it again answers
  `Lakana is already running from ...` and changes nothing. If the command has ended, the
  instance is down: run `lakana server start --local` again.
- **A Compose instance.** The worker is a service of its own. A stopped one is started with
  `docker compose up -d` (it starts whatever your `.env` asks for); one that a restart policy brings back needs only a moment.

## Which worker takes which work

A worker can take interactive work, pipeline work or both (`LAKANA_WORKER__TAKES`, shown as
`takes` in `lakana worker list`). On most instances every worker takes both, and nothing in this
section applies.

- **Interactive work:** ad-hoc queries, writes and views that a person or a tool submits.
- **Pipeline work:** deploys, the tasks of pipelines and the queries they run, and table
  maintenance, also the maintenance a person starts by hand. A maintenance run stops at the first
  safe point after 30 minutes (`LAKANA_RUN__MAINTENANCE_TIMEOUT`), so a task's input query that
  waits behind one waits about that long.

A query or deploy of one kind waits as long as no worker that takes its kind is free, even when
workers of the other kind are idle. When no running worker takes the kind at all, a query, a
maintenance run, a deploy or a task says `no_worker_of_kind`. If the wait does not
end, ask the operator to check `lakana worker list`: at least one worker must take that kind. The operator's side is in
[Separate workers for ad-hoc queries and pipelines](deploy-with-compose.md#separate-workers-for-ad-hoc-queries-and-pipelines).

## What an operator can look at

`lakana worker list` shows the workers that registered, when each was last heard from, the work it
takes (`takes`) and what it does. A worker in the state `lost` has not sent a heartbeat for the lease:

<!-- source: docs/examples/getting-started/run.sh#worker-list -->
```bash
lakana worker list
```

`lakana query list` shows every queued and running query of an environment, in the order the
workers will take them, with the reason each waits:

<!-- source: docs/examples/getting-started/run.sh#query-list -->
```bash
lakana query list --env dev --all
```

`lakana scheduler status` shows which worker holds the scheduler role and how many runs and tasks
are active, pending, ready and running:

<!-- source: docs/examples/getting-started/run.sh#scheduler-status -->
```bash
lakana scheduler status
```

`lakana worker list` and `lakana scheduler status` need `MANAGE_ENVIRONMENT` on the instance.
A local instance's own user has it.

## A tool that runs queries in parallel

The limit per user counts the ad-hoc queries of one account, over every environment. A script, a
dashboard or a BI tool that runs many queries at once under one service account is one user: its
queries beyond the limit wait, with `user_limit` while a worker is free (and `workers_busy` while all are busy), which you see in `waiting_for` of the status and
on standard error of `lakana sql`. Raise `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL` (a number
at or above the number of workers means no limit), or give each tool an account of its own.

## The numbers are estimates

`workers`, `ahead` and `running` are counted when you ask, and they are close, not exact:

- A worker counts as running until it has been silent for `LAKANA_RUN__LEASE_SECONDS` (60 s). For
  up to that long after a worker died it still counts, and if it held a query it counts as busy.
  A reason may therefore say `workers_busy` for up to a minute after the last worker died, before
  it says `no_worker`. The query that worker ran also still counts against the limit per user
  until it is ended, about a minute later.
- A worker that is shutting down still counts.
- `workers` counts the running workers that take this kind of work; `ahead` counts the queued
  queries and deploys of every environment that were queued earlier and that those workers take
  first, because workers take them in that order. Queries of other people count, too, also those
  of a person who is held back by the limit, although they will be passed by.
- With `auto`, the limit per user follows the workers that take interactive work and are running
  now: it can change when a worker starts or stops. The sentence states the limit the server
  reads; the workers apply it only when they read the same `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL`.
- A listing counts at most 10000 ahead and then says `more than 10000`.

## When a worker is lost

A worker is lost when it was killed, lost power, or could not reach the database for longer than
the lease. The platform then ends what it held, so that nothing stays `RUNNING` for ever:

- A query that ran on it is ended after about a minute (`LAKANA_RUN__LEASE_SECONDS`), up to about
  half a minute more when the scheduler had paused or had just taken over.
  "About a minute" holds when another worker can take over the scheduler's role. PostgreSQL is
  asked to probe every client and to drop a connection whose machine no longer answers within
  about two thirds of a lease, so the role of a worker that lost power or its network is expected
  to be freed within about a lease.
  If PostgreSQL refuses the request, a warning is logged once at the start and the role of such a
  worker can stay with it for hours. `lakana scheduler status` shows which worker holds the role and when it last reported; if that worker is gone and the role stays with it for much longer than a minute, ending its database session in PostgreSQL (`pg_terminate_backend`), or restarting PostgreSQL, frees it; restarting the other workers does not.
- A worker that is alive but could not write the final status of a query or a deploy ends that run
  itself when it is idle again.
- A local instance after the machine slept ends a running query only if its worker sends no
  heartbeat within about half a minute after waking. Run it again then.
- A read, a write that had not been committed and a maintenance run end `FAILED` with the cause
  `worker_lost` and a message that says to run it again. A write that had been committed ends
  `SUCCEEDED`, so a repeat would write it twice; for a pipeline's SQL task the task is `succeeded`
  too and is not run again, unless its table has [checks](check-your-data.md): that task is taken
  again and runs only its checks, because a table that was written and not checked must not count
  as done. A deploy ends `failed`, nothing was activated,
  and the message says to deploy again.
- A pipeline task whose table may hold rows that no check has passed (a Python task with checks
  lost after its last attempt's write, or a task with checks lost in a run that you cancel, also
  when you cancel after the task waits again) is not ended at once: it waits with the reason
  `setting_back` and the next free worker sets the table back first.
  [Check your data](check-your-data.md#a-worker-that-is-lost) has the messages.

[Stop a query](stop-a-query.md#a-worker-that-is-lost) has the messages and the details of each
case, and [Projects and deploys](projects.md#deploy-dry-run-roll-back) the one for deploys.
