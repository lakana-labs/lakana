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

A status that has ended lists the queries that ended in the last 24 hours, without `--all`.

## Find the deploys of one repository

<!-- source: docs/examples/getting-started/run.sh#find-repo-deploys -->
```bash
lakana deploys list --env dev --repo analytics --limit 20
```
