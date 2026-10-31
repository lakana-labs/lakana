# Why did it fail?

Every task of a run that fails, is cancelled or never starts carries a stable code, a sentence for the
cause and a sentence for the next step. The table below lists every code once. For a task that
waits instead, see [Why is my query or task waiting?](../how-to/why-is-it-waiting.md).

The code never changes. The sentences may be reworded in a later release, so a program branches on
the code and shows the sentences to a person. The free text of the failure, with the details of this
one (a table name, a check, an error), stays in `detail` / `error_message`.

## Where you see it

| You use | The failure appears as |
|---|---|
| `lakana runs show RUN_ID` | The `error` column of each failed task, and under the table, for each failed task (at most 50), a block: `<asset> failed: <code>`, the cause and `Next:` and the next step. |
| `lakana run` | When a task fails, its progress line ends with `failed: <code>. Next: <next step>`, and the summary at the end has the block of `lakana runs show` for each failed task. A task that waits to try again, one that never started and one that was cancelled print no next step. |
| `lakana runs show RUN_ID --output json` and the REST API | `failure` on each task and on each attempt in `GET .../runs/{run_id}/tasks/{asset}/log`: an object with `code`, `cause` and `next_step`, or `null` for a task with no error. `error_message` keeps the free text, as before. `failed_tasks` on a run counts the tasks that ended `failed`; it is also in the run list. |
| The SDK | `RunTask.failure` and `TaskAttempt.failure` (a `Failure` with the same three fields), `RunTask.error_message` and `Run.failed_tasks`. |

## The codes

A code belongs to one task, or to one attempt of a task; a run has none of its own. An attempt that
is tried again carries its own code, and so does the task while it waits for the next attempt.

| Code | What it means | What to do |
|---|---|---|
| `build_failed` | The task's code, or the Python environment it needs, failed. It covers a Python asset that raised an error or ended without writing its table, an environment that could not be built (a package that does not install, a lock file written for another platform version), a name that something else holds (a view where the asset writes a table) and a process that was killed. | Read the task's log with 'lakana runs logs RUN_ID --task ASSET', fix the cause and deploy again. The log of the failing attempt has the error and the output of the process. |
| `code_changed` | The deployed code is missing, or no longer matches what was deployed. It covers the code integrity check (the stored code no longer matches the digest recorded at deploy; the run is stopped), a code archive that is missing or unsafe, and a project version whose record no longer fits (a missing SQL text or asset file, inputs of another kind than the catalog has now). | Deploy the project again; if it keeps happening, ask the operator to check the code storage. |
| `secret_refused` | A package index refused the credential, or has none registered. A package index asked for a credential and none is registered for it, it refused the one that is (HTTP 401 or 403), or a package has no wheel while a credential is in play. | Ask an administrator to check the index's credential secret with 'lakana index update NAME --credential-secret SECRET', then run again. The detail names the index and the secret. |
| `no_runtime` | This worker cannot run this kind of task. It covers a worker without a Python runtime, a task directory that is not private, a task token lifetime setting that is too low for the asset's timeout, a manifest this worker is too old to read, a Python task of a protected environment on a worker without containers (`LAKANA_RUN__RUNNER=local`), and a run that a server older than the worker started for a project with checks. | Ask the operator to run a worker that can (the detail names what is missing), then run again. The detail names the setting. |
| `upstream_failed` | A task this one reads from did not succeed, so this one never ran. This task did not start, and nothing is wrong with it. | Fix the upstream task first, then continue the run with 'lakana runs resume RUN_ID'. |
| `upstream_missing` | A table or artifact this task reads has not been built. The table or artifact version the task reads does not exist, because the run did not select the task that builds it and it was never built. | Run the task that builds it first, or run the whole project. |
| `check_blocked` | A check of the table failed, so the data was not published. It covers a `block` check, and a `keep` check, which leaves the data in the table. A check that failed beside one that could not run is `check_blocked` too. | Read the failed checks with 'lakana runs show RUN_ID', fix the data or the check, then run again. The failed checks, their counts and what became of the table are in the detail and in the check results of the run. |
| `check_error` | A check could not be counted, so the data was not judged. Every failing check of the task errored: its SQL failed or ran past its limit, or the run's principal may not read a table it names. A check that could not run counts as failed for a `block` or `keep` check. | Read the check's error with 'lakana runs show RUN_ID', fix its SQL or the access it needs, then run again. |
| `checks_unfinished` | The checks of the table did not finish in 5 more tries after its write, so Lakana stopped trying and set the table back where it could. The table had been written and its checks had no verdict yet. Each claim that only ran the checks was lost with its worker (often a check that needs more memory than the worker has), or the checks could not start (the run's principal no longer exists, or the worker cannot read the deployed project version), or counting a check kept failing for a temporary reason, which can happen only when a project asks for more than 5 retries. The task's message says what became of the table; when the table could not be set back, it says so and the table may still hold rows no check has passed. See [A run that does not end](../how-to/check-your-data.md#a-run-that-does-not-end). | Read the attempts with 'lakana runs logs RUN_ID --task ASSET': for a lost worker, raise its memory limit or make the check cheaper; for checks that could not start, bind a run principal or upgrade the worker as the message says; for a count that kept failing, check that the object store and the database answer. Then run again. The job's notification lists such tables under `checks_unfinished`. |
| `query_invalid` | The task's SQL is invalid or was refused. The SQL of a task failed to parse, named something that does not exist in a way the engine refuses, or produced a result the platform will not hold (too large). | Read the message in the task's log, fix the SQL and deploy again. |
| `permission_denied` | The principal that runs the task may not do what the task needs. It covers a read or a write that the run's principal may not do, and a run principal that was deactivated or deleted. | Ask an administrator to grant the access, or to bind another run principal with 'lakana deployments set-run-as'. |
| `not_found` | Something the task needs does not exist. A table, view, artifact or the run itself was gone when the task needed it. | Check the names in the detail; build the missing object or deploy the project again. |
| `memory` | The task used more memory than the worker allows, so it was stopped. The worker stopped the task when the worker passed its memory limit (`LAKANA_WORKER__MEMORY_LIMIT`). A task that may have written is set back first (`setting_back` in [Why is my query or task waiting?](../how-to/why-is-it-waiting.md#a-waiting-task)). | Make the task handle less data at once, or ask the operator to raise the worker's memory limit. |
| `timeout` | The task ran longer than its time limit. Either the asset's own timeout or the largest timeout the worker allows, whichever is shorter. | Make the task faster, or raise its timeout in the project and deploy again. |
| `cancelled` | The run was cancelled or stopped before the task finished. A person cancelled the run, or a failed code integrity check stopped it. The tasks that were still waiting are `cancelled` too. | If a person cancelled it, start the run again when you want it; if another task of the run failed with `code_changed`, deploy the project again first. |
| `worker_lost` | The worker running the task stopped before it finished. The worker was killed, lost power or its network, or was stopped for memory from outside; the platform saw that its lease ran out. | Run again; if it repeats, ask the operator to check the worker's log and memory limit. A task is tried again while it has attempts left, so a task carries this code only when its last attempt was lost; each lost attempt carries it. |
| `conflict` | Another writer changed the table at the same time, or a Python task's table holds no write of its own (another writer, a server older than the worker, or maintenance). When two writers committed to the same table and this one lost, the platform already retried once without counting an attempt; a Python task whose table holds no write of its own is not retried. | Run again; if it repeats, find what else writes this table. For a Python task, the message names its causes, and `lakana audit list --env ENV` shows who wrote (it needs `READ_AUDIT_LOG`: ask an administrator if you do not hold it); if the server runs an older release than the worker, upgrade them together. |
| `internal` | The platform failed in a way it did not expect. Anything the platform did not expect, and a code that a newer release added and this client does not know. | Read the task's log; if the cause is not clear, ask the operator for the worker's log and report it with the run id. |

## Old runs: `unknown`

A task that failed before failure codes existed has no stored code. It shows `unknown` with a generic
cause and next step, and its `error_message` is unchanged. `unknown` is only what is shown for such a
row; it is never stored, and a new run never has it. A task that was `upstream_failed` or `cancelled`
in an old run shows that code, because its status says it exactly. After an upgrade, nothing is
converted: the old rows stay as they are.

## Which codes a failed run has

- A run is `failed` when a task failed or could not start. The task that failed has its own code; the
  tasks below it have `upstream_failed`.
- A cancelled run has `cancelled` on every task it ended, and a task whose worker was lost after it
  had written is first set back (see [A worker that is lost](../how-to/check-your-data.md#a-worker-that-is-lost))
  and then ends with the code of why.
- `lakana runs resume RUN_ID` starts a new run of the tasks that did not succeed; each gets a fresh
  record.

See also [Check your data](../how-to/check-your-data.md) for the checks and [Stop a query](../how-to/stop-a-query.md) for
cancelling.
