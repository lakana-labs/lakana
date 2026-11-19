# Find a run, a query or a deploy

Look up what happened in an environment: the runs that failed since yesterday, the queries one
person submitted, the deploys of one repository. For every filter, the order and the paging, see
the [reference](../reference/lists.md).

## Find a failed run since yesterday

`lakana runs list` takes a status and a time. `--since` accepts a date, which is read in your time
zone, or a full time such as `2026-10-18T09:30:00Z`. `date` writes the time of 24 hours ago, which lists what failed since then. On macOS, write `date -u -v-1d +%Y-%m-%dT%H:%M:%SZ` instead:

<!-- source: docs/examples/getting-started/run.sh#find-failed-run -->
```bash
lakana runs list --env dev --status failed --since "$(date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%SZ)"
```

When nothing failed, the list is empty. Add `--job nightly` to see one job's runs, `--trigger
schedule` for the runs the scheduler started, and `--repo analytics` for one repository.
`--status failed,cancelled` shows both. Take the id of a run from the list and open it with
`lakana runs show RUN_ID --env dev`; a failed task shows its code and what to do about it ([Why did it fail?](../reference/why-did-it-fail.md)).

## Find what failed, in every environment

`lakana runs failed` lists the jobs whose newest finished run failed, in every environment you may
read. It is the list that Needs your attention on Home shows: per job (or per repository, for runs
that belong to no job) it takes the newest run that finished in the last 7 days, so a later
success or cancelled run hides an earlier failure. Each line says what failed and what to do:

<!-- source: docs/examples/getting-started/run.sh#find-failed-everywhere -->
```bash
lakana runs failed
lakana runs failed --env dev --limit 20
```

A run that failed without a failed task, such as one whose code changed after the deploy, prints
its own message. When more failed than fit, the list says so; narrow it with `--env`. To list
every run of every environment, for example those still running:

<!-- source: docs/examples/getting-started/run.sh#find-runs-everywhere -->
```bash
lakana runs list --all-environments --status running
```

`--all-environments` and `--env` do not go together. Home shows the same failed runs; in the SDK
they are `client.runs.failed()` and `client.runs.list_all()`.

In the web interface, the Jobs page lists the same failed runs under **Failed, needs attention**,
in every environment you may read or in the one chosen under **Environment**. Each row says why
the run failed without a click: the code of the task that failed first, its cause and the next
step, or the run's own message when no task failed. **Resume** starts the failed part again and
**Logs** opens the task that failed. **Running now**, above it, lists the queued and running runs
with how far each has come, and **Cancel** asks a run to stop.

A failed run under **Needs your attention** on Home opens the run's page. A
job's page, opened from **Your work** on Home, lists the job's runs under **Runs** and shows the
one you choose below the list. A run lists its tasks with their state, duration and rows; a task
that failed or never ran has a second line with its code, the cause and the next step. **Logs**
opens the task that failed first, and a task's name opens its page: its attempts with the memory,
processor time and bytes each used, and its log, which the page follows while the task runs and
offers as **Save log** once it is complete.

The **Runs** tab of the Jobs page lists every run of the environments you may read, newest first,
50 at a time with **Show more**. Narrow it by **Environment**, **State** (**Queued or running** is
one choice), **Trigger**, **Job** (an exact name, applied when you press Enter or leave the
field) and **Period** (the last 30 days unless you choose another). The address holds every
filter, so a bookmark brings them back, and Back undoes one filter at a time, as on a job's page,
without leaving the tab; an address without an environment shows all of them. A run you started
from the command line for a selection of assets says `selection` and its patterns, with
`+upstream` and `+downstream`; a resume says which run it continues and links to it. The tab starts no run: start a selection with
`lakana runs start`. The list is read again every 30 seconds until you show more; then **Read
again** starts over from the first page. **See every running run**, under **Running now** when
more run than it shows, opens the tab filtered to queued and running runs.

## See what a task used

`lakana runs show RUN_ID --env dev` ends the task table with a block that has one line per attempt,
for example `silver.clean attempt 1: 412 MiB · 3.1 s CPU · 1.2 GiB read`: the highest memory the
attempt's process reached, the processor time it used, and the bytes it read. A dash means nobody
measured it, and `0 B` means it was measured and nothing was read; a task that has not started has
no line. Use it to see which task needs the most memory before you set a limit for it. A query
that read only file metadata, which DuckDB answers without reading data, shows no bytes read.

`--output json` has the raw numbers in `attempts` on each task: `attempt`, `peak_memory_bytes`,
`cpu_seconds` and `bytes_read`, each `null` when unknown. The same three fields are on each attempt
of `GET /api/v1/environments/{env}/runs/{run_id}/tasks/{asset}/log`, on the status of a query
(`GET /api/v1/environments/{env}/queries/{query_id}`), in the SDK (`Run.tasks[].attempts`,
`TaskAttempt`, `QueryStatus`), and are described in [the OpenAPI document](../reference/openapi.json).

## Page through a long list

A list shows 100 rows unless you ask for fewer with `--limit`. When there are more, it ends with a
line that is the command for the next page:

<!-- output -->
```text
More results: lakana runs list --env dev --status failed --limit 50 --cursor CURSOR
```

Copy that line. The cursor goes with the same filters and the same limit. With `--output json`
the line goes to stderr and stdout stays one list.

## Find the queries of one person

Someone who manages the environment sees every query, and `--submitter` narrows them to one
principal. Everyone else sees their own queries only, and the name of someone else shows nothing:

<!-- source: docs/examples/getting-started/run.sh#find-person-query -->
```bash
lakana query list --env dev --status failed --submitter admin
```

A status that has ended lists the queries that ended in the last 24 hours, without `--all`;
add `--since 2026-10-01` to look further back. `--all-environments` looks in every environment
you may query at once.

## Find the deploys of one repository

<!-- source: docs/examples/getting-started/run.sh#find-repo-deploys -->
```bash
lakana deploys list --env dev --repo analytics --limit 20
```

In the web interface, the **Deploys** tab of the Jobs page lists one environment's deploys at a
time, newest first, 50 at a time with **Show more**: the result, the kind, the commit, the
repository, who asked for it and when, and the first line of a failed deploy's message. The
repository's current version says `current`. Choose the repository under **Repository**; a job's
page links to its repository's deploys from its facts. **Roll back to this…** beside an earlier
successful deploy rolls the repository back to it (see
[Deploy, dry-run, roll back](projects.md#deploy-dry-run-roll-back)).
