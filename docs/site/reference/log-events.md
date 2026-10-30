# Log events

The log lines an operator may want an alert for end with a stable code, `event=<code>`. Match an
alert rule on the code, not on the wording: the wording of a line may change in any release, a
code does not. A code is never renamed or given another meaning; a release that removes one says
so in its [upgrade notes](../explanation/upgrades.md).

A line with a code looks like this (the server and the worker write to standard error;
`docker compose logs worker` shows it):

<!-- output -->
```text
2026-10-21 06:31:06,239 WARNING lakana_worker.heartbeat: No heartbeat was written for 60 s (LAKANA_RUN__LEASE_SECONDS): this worker claims nothing and stops the query it runs until one is written. Check that the database is reachable. event=heartbeat_missed
```

The code is the last word of the message's first line; a traceback, when the line has one, follows
on the next lines. A program that embeds the server or the worker with its own logging handler
finds the code in the record's attribute `event`.

## The codes

| Code | Written by | Level | The line starts | What it means and what to do |
|---|---|---|---|---|
| `memory_stop` | worker | ERROR | `Memory limit exceeded: the worker and its tasks use` … `The worker stops now.` | The worker passed 90 percent of its memory limit with nothing left to end, and stopped; Docker starts it again. What it ran is marked failed. Narrow the work or raise `LAKANA_WORKER_MEMORY`: [Memory limits](../how-to/deploy-with-compose.md#memory-limits). |
| `memory_task_ended` | worker | ERROR | `Memory limit exceeded: the worker and its tasks use` … `The largest consumer` | The worker ended its largest query or task to stay under its memory limit and goes on. The run shows the code `memory`. Many in a row: as for `memory_stop`. |
| `database_unreachable` | worker | ERROR, then WARNING | `The worker's heartbeat could not be written`, then `The worker's heartbeat is still failing` | The worker cannot reach the catalog database. Check the database and the network between them. |
| `heartbeat_missed` | worker | WARNING | `No heartbeat was written for` | The worker has not reached the database for a lease (`LAKANA_RUN__LEASE_SECONDS`, 60 s): it claims nothing and stops its query; its runs are ended as lost. |
| `heartbeat_recovered` | worker | INFO | `The worker's heartbeat is fresh again` | The worker reaches the database again and claims work again. Ends a `heartbeat_missed`. |
| `worker_lost` | worker (the one with the scheduler role) | WARNING | `Ended` … `whose worker stopped reporting` | Queries or deploys of a worker that stopped reporting were ended; they show `worker_lost`. Look at that worker's log and memory limit. |
| `lease_lost` | worker | WARNING | `Task run <id> is no longer ours` | The worker's claim on a task ran out before the task finished, so its outcome was dropped and another attempt runs. Usually follows `heartbeat_missed`. |
| `lease_renewal_failed` | worker | ERROR | `Lease renewal of task run <id> failed; retrying.` | The worker could not extend its claim on a task. If it keeps failing the task is taken from it (`lease_lost`). |
| `storage_unreachable` | server, worker | WARNING | `The storage of environment`, or `… a file of an artifact upload failed`, `Opening a file of artifact` | An environment's object storage did not answer, or refused access to the orphaned-locations walk, so a table, an artifact file or a directory without a record was not read or written. Check the storage (`docker compose logs seaweedfs`) and its credentials. |
| `readiness_failed` | server | WARNING | `The instance is not ready: /readyz answers 503.` | `/readyz` turned to `503`; the line names each failed check and why. Logged once when it turns, not on every request: [The health routes](../how-to/instance-health.md#the-health-routes). |
| `readiness_recovered` | server | INFO | `The instance is ready again: /readyz answers 200.` | Ends a `readiness_failed`. |
| `scheduler_role_taken` | worker | INFO | `This worker holds the scheduler role now.` | This worker now ends the runs of lost workers, starts scheduled jobs and runs the hourly sweep. |
| `scheduler_role_lost` | worker | WARNING | `This worker no longer holds the scheduler role` | Its database connection failed, or another worker took the role over. Another worker logs `scheduler_role_taken` within seconds; if none does, no worker holds the role (`lakana scheduler status`). |
| `scheduler_step_failed` | worker | ERROR, then WARNING | `Scheduler step failed; continuing.`, then `Scheduler step still failing` | The worker with the scheduler role could not run a step, usually because the database failed. It tries again with a growing pause. |
| `sweep_failed` | worker | WARNING or ERROR | `The <kind> sweep failed`, `The table column backfill failed`, `Housekeeping failed; continuing.` | One part of the hourly sweep failed; the others still ran, and it is tried again at the next sweep: [The sweep](../how-to/deploy-with-compose.md#the-sweep). |
| `sweep_hung` | worker | WARNING | `Housekeeping has been running for` | A sweep has run for more than twice its time limit, and no further sweep starts until it ends. It may be waiting on the storage or the database. |
| `poll_failed` | worker | ERROR | `Polling iteration failed; continuing.` | The worker's loop that looks for work hit an error; the traceback follows. Occasional lines are harmless; a stream of them is not. |
| `internal_error` | worker | ERROR | `Running task run <id> failed unexpectedly.`, `Preparing task run <id> failed unexpectedly.`, `Deploy run <id> failed unexpectedly.` | The platform failed in a way it did not expect. The run shows the code `internal`; the traceback follows the line. |
| `package_cache_trimmed` | worker | INFO | `The package cache was trimmed from` | The worker's housekeeping brought uv's package cache below `LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES`; the line has the sizes before and after. Nothing to do: [Disk use](../how-to/instance-health.md#disk-use). |
| `package_cache_over_limit` | worker | WARNING | `The package cache is` … `after a trim` | A trim left uv's package cache above its limit: every package left was downloaded within `LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS`, a `uv cache` command failed, or the trim ran out of time. The line names which. Raise the limit or give the `worker-cache` volume more space: [Disk use](../how-to/instance-health.md#disk-use). |

## See also

- [Is my instance healthy?](../how-to/instance-health.md): the other signals, and what to collect
  before you ask.
- [Why did it fail?](why-did-it-fail.md): the codes a failed task shows.
