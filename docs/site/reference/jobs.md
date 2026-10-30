# Jobs

What `GET /jobs` and `lakana job list` show for a job, and in particular the triggers that start
it: the fields, each kind of trigger, the route, the commands and the SDK. For the steps, see
[Run a job on a schedule](../how-to/run-a-job-on-a-schedule.md).

## Triggers

Every job has a list `triggers`, the ways it starts. The list is read-only: a schedule and
`on_deploy` are declared in `lakana.yaml` and changed by a deploy, and pausing a schedule is a
separate call (`lakana job pause`). Schedule comes first, on demand last.

| `kind` | Present | Fields |
|---|---|---|
| `schedule` | when the job has a schedule in this environment | `cron`, `timezone` (IANA), `paused` (true or false), `next_run_at` (UTC; `null` while paused) |
| `deploy` | when the job has `on_deploy: true` in this environment: it starts after each deploy that makes another project version current, as the deployer | none |
| `on_demand` | always: every job can be started through the API, the SDK or the CLI | none |

`next_run_at` is the time the scheduler will fire next, the same value as the job's `next_run_at`;
a paused schedule fires at no time, so it is `null`.

Triggers may gain kinds in a later release, so **a client must ignore a `kind` it does not
know**. The SDK does so.

## REST

| Route | Needs | Answer |
|---|---|---|
| `GET /api/v1/environments/{env}/jobs?repository=` | `READ_ENVIRONMENT` | a list of jobs, by repository and name |
| `GET /api/v1/environments/{env}/jobs/{job}?repository=` | `READ_ENVIRONMENT` | one job, the same body as its entry in the list |

Both answer the fields `name`, `environment`, `repository`, `select`, `upstream`, `downstream`,
`cron`, `timezone`, `state`, `paused`, `next_run_at`, `next_run_local`, `waiting_for`,
`waits_for_run_id`, `last_run_id`, `last_run_status`, `last_run_trigger`, `last_run_at` and
`triggers`. `cron`, `timezone` and `state` are unchanged. The pause and resume routes answer
the same body.

`repository` is needed only when several repositories declare a job of the same name; without
it the answer is `409` and names them. A job that does not exist is `404` and lists the jobs that
do.

## Commands and SDK

| Command | Shows |
|---|---|
| `lakana job list [--env ENV] [--repo REPO]` | one row per job with a `triggers` column that holds each trigger on its own line, for example `schedule 0 6 * * * Europe/Berlin`, `after each deploy` and `on demand`; a paused schedule reads `schedule 0 6 * * * Europe/Berlin (paused)` |
| `lakana job show NAME [--env ENV] [--repo REPO]` | the job's state, selection and last run, and each trigger on its own line with the next run of a schedule |

With `--output json` both print the whole body, `triggers` included.

`client.jobs.list(environment)` and `client.jobs.get(environment, job, repository=None)` return
`Job` objects whose `triggers` is a tuple of `Trigger` (`kind`, `cron`, `timezone`, `paused`,
`next_run_at`; the fields a kind does not have are `None`). From a server that does not send
triggers the tuple is empty.

## After a deploy

A deploy that makes another project version current starts each job with `on_deploy: true`, as
the principal that deployed, through the same call as `POST .../jobs/{job}/runs`. The run's
`trigger` is `deploy`; `lakana runs list --trigger deploy` and `GET .../runs?trigger=deploy` find
these runs. The deploy records what came of each start in `job_starts`, a list by job name in the
body of `GET /api/v1/environments/{env}/deploys/{id}` and of the deploy history. It is empty for a
dry run, a rollback, a deploy that failed and a deploy of the commit already current.

| Field | Meaning |
|---|---|
| `job` | the job's name |
| `outcome` | `pending` (the worker has not started it yet), `started`, `waiting` (the project was busy; the start waits for the active run) or `not_started` |
| `run_id` | for `started`, the run |
| `waits_for_run_id` | for `waiting`, the active run the start waits for |
| `reason` | for `not_started`: `lacks_run` (the deployer may not `RUN` in the environment, or deployed with a CI token whose trust rule does not opt in with `start_on_deploy_jobs`), `refused` (refused as a start by hand would be: no run principal in a protected environment, an inactive one, a selection that matches nothing; or, for a CI deploy, the job no longer has `on_deploy: true` in the project version current when it starts), `error` (the worker's log has it) or `lost` (the worker stopped before it recorded the start, so the job may or may not have started: look for its run with `lakana runs list --repo REPO --trigger deploy --job JOB` before you start it by hand; shown once the deploy is older than the run lease, `LAKANA_RUN__LEASE_SECONDS`) |
| `message` | for `not_started`, one sentence with the cause and the next step |

`lakana deploy` waits until no start is `pending`, then prints the runs and the waiting starts
under `Jobs started after the deploy:` and a `warning:` line on stderr for each job not started.
The deploy itself succeeded either way: its exit code is 0. `client.deploys.wait` waits the same
way, and `Deploy.job_starts` is a tuple of `DeployJobStart` with the fields above.

With `--timeout`, a deploy that succeeded before the worker recorded every start still exits 0. It
prints what it knows, and a warning for each start not recorded yet:
``warning: the start of job `nightly` is not recorded yet. See what came of it with 'lakana deploys show <id> --env prod'.``
Do not start such a job by hand before you have looked: the worker may still start it. In Python,
`client.deploys.wait(..., timeout=...)` raises `lakana.JobStartsNotRecorded` then, a
`TimeoutError` whose `deploy` is the succeeded deploy with those starts `pending`. A deploy that
has not finished by the timeout still exits 1 (`TimeoutError` in Python).
