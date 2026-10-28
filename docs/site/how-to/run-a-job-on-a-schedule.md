# Run a job on a schedule

After this guide you can declare in `lakana.yaml` what to build and when, deploy it, give the
schedule an identity to run as, see when each job runs next, pause it, and start the same job from
CI. You also know what happens when the project is busy at the time, and when the platform was
down.

In short:

- A **job** is a name for a selection of assets, with an optional schedule. It lives in
  `lakana.yaml`, so a deploy changes it together with the code. A job can also start after each
  deploy.
- A scheduled run starts **as a service account** that an administrator binds to the deployment.
  Without one, nothing runs by itself.
- Only one run of a project is active at a time. A start that meets an active run **waits** and
  begins when the project is free; at most one start waits per job.
- After downtime a job runs **once**, for the latest time it missed. There is no catch-up.

You need a project that you have deployed once (see
[Getting started](../tutorials/getting-started.md)). The guide works on one small project, the
example `docs/examples/run-a-job/` of the Lakana repository: two assets and two jobs. Every
command below is run against a local instance by the documentation's tests.

## Declare a job

Add a `jobs:` section to `lakana.yaml`:

<!-- source: docs/examples/run-a-job/project/jobs.yaml -->
```yaml
jobs:
  daily_silver:
    select: ["silver.*"]
    upstream: true
    schedule:
      cron: "0 6 * * *"
      timezone: Europe/Berlin
  hourly_bronze:
    select: ["bronze.*"]
    schedule: { cron: "15 * * * *" }
environments:
  dev:
    jobs:
      hourly_bronze: { schedule: null }
```

- `select:` takes the patterns of `lakana run --select`; `upstream:` and `downstream:` are the
  flags of the same names. A deploy refuses a selection that matches no asset.
- `daily_silver` builds `silver.*` and what it reads, every day at 06:00 Berlin time.
- `hourly_bronze` builds `bronze.*` at 15 minutes past every hour, but not in `dev`: the override
  `schedule: null` removes its schedule there. The job still exists in `dev`, and you can start it
  by hand. An environment can change a job the top level declares, never add one.
- A job without `schedule:` has no timer: it starts only by hand or through the API.
- The time zone is an IANA name such as `Europe/Berlin`; `UTC` when you leave it out.

The full list of keys and the messages for a mistake are in [Jobs](../reference/lakana-yaml.md#jobs). The rules for
the `cron:` text, for daylight saving time and for the two day fields are in
[Cron and time zones](#cron-and-time-zones) below.

## Deploy, and read the dry run

Commit the file. A dry run shows what the deploy would do, schedules included, before you merge:

<!-- source: docs/examples/run-a-job/run.sh#dry-run -->
```bash
lakana deploy --env dev --dry-run
```

<!-- output -->
```text
Queued dry run 5e33fdd8-8103-4bd2-adb8-fadeaa707f42
Dry run of readings in dev at 24bc0c3b3040b76773d8ada2381a4e16ad3ce8db
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ change ┃ table                       ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ added  │ bronze.readings             │
│ added  │ silver.station_temperatures │
└────────┴─────────────────────────────┘
Jobs:
  added daily_silver
  added hourly_bronze
warning: job `daily_silver` has a schedule but no run principal is bound in `dev`; it would not run until an admin binds one with `lakana deployments set-run-as`
```

The section `Jobs:` lists the jobs added, removed and changed; a changed job names what changed:
the selection, the cron expression (a schedule added or removed counts) or the time zone. A new
schedule starts counting at the deploy: its first run is the next time after it, with no catch-up
from before. A changed cron or time zone counts from the deploy as well.

Then deploy:

<!-- source: docs/examples/run-a-job/run.sh#deploy -->
```bash
lakana deploy --env dev
```

### The warnings of a deploy

A deploy prints a `warning:` line on stderr for each of these (`--output json` has them in
`warnings`). A dry run words them as what a deploy *would* do.

| Warning | What it means | What to do |
|---|---|---|
| ``job `daily_silver` has a schedule but no run principal is bound in `dev`; it does not run until an admin binds one with `lakana deployments set-run-as` `` | The job has a schedule in this environment, but nothing for it to run as. The dry run says `it would not run`. | Bind a principal: [next section](#bind-a-run-principal). The warning is stored with the deploy and stays on its record even after you bind one later; to see the current state, run `lakana job list`. |
| ``job `daily_silver` is not in this manifest and a start of it is waiting for the active run; that start is dropped; add the job back and deploy again, or start its assets with `lakana run --select` `` | The deploy removes a job whose start was waiting for the active run. The waiting start goes with the job. The dry run says `that start would be dropped`. | Nothing, if you meant to remove the job. Otherwise add it back and deploy again. |
| ``job `daily_silver` notifies through secret `team-alerts`, which does not exist in `dev`; it does not notify until you create it: …`` (or ``… which is revoked in `dev` …``) | The job names a webhook secret that is missing, or revoked, in this environment. The deploy goes through and the job does not notify. The dry run says `would not notify`. | Create the secret, or for a revoked one a secret under another name that you put in `notify.webhook`: [Get told when a job fails](get-told-when-a-job-fails.md#create-the-secret). |
| One `warning:` per table that is kept after a failed check and whose asset the deploy removes | Unchanged; see [Check your data](check-your-data.md). | |

`lakana undeploy` removes the jobs of the deployment and drops a waiting start **without** a
warning: you asked for the deployment to go.

## Bind a run principal

A scheduled run never runs as a person. It writes as the service account bound to the deployment,
so what it may read and write is exactly what you granted that account. An administrator
(`MANAGE_ENVIRONMENT`) creates the account, grants it what the assets need and binds it:

<!-- source: docs/examples/run-a-job/run.sh#bind -->
```bash
lakana principal create etl-dev --type service-account
lakana grant create --principal etl-dev --role DATA_ENGINEER --kind ENVIRONMENT --env dev
lakana deployments set-run-as readings --env dev --principal etl-dev
```

The binding belongs to the deployment and survives deploys and rollbacks. The principal must be an
active service account. [Run principals](projects.md#run-principals) has the rest. Times that pass
while no principal is bound are not caught up: after you bind one, the next time fires, not the
ones before it.

## See the jobs

<!-- source: docs/examples/run-a-job/run.sh#list -->
```bash
lakana job list --env dev
```

<!-- output -->
```text
┏━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ name          ┃ state       ┃ triggers                         ┃ next_run                                   ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ daily_silver  │ scheduled   │ schedule 0 6 * * * Europe/Berlin │ 2026-10-15 06:00 Europe/Berlin (04:00 UTC) │
│               │             │ on demand                        │                                            │
│ hourly_bronze │ no schedule │ on demand                        │                                            │
└───────────────┴─────────────┴──────────────────────────────────┴────────────────────────────────────────────┘
Not shown: repository, last_run. Use --output json to see them.
```

`triggers` lists how each job starts: its schedule (marked `(paused)` while paused), `after each deploy` for a job with `on_deploy: true`, and `on demand`, which every job has. The next run is shown in the job's time zone and in UTC. `last_run`, hidden when the terminal is narrow (`--output json` has it), shows the status of the latest
run and what started it (`schedule`, `api`, `manual` or `deploy`). The list shows every job of the
environment to anyone who may read it; `--repo` limits it to one repository.

| State | Meaning | What to do |
|---|---|---|
| `scheduled` | It fires at the next run shown. | Nothing. |
| `paused` | The schedule is paused; nothing fires. | `lakana job resume`. |
| `waiting` | A start waits for the active run of the project. | Nothing; see [When the project is busy](#when-the-project-is-busy). |
| `waiting to start` | A start is waiting, the project is free, and the scheduler has not looked yet. | Nothing: it begins within a few seconds. |
| `no schedule` | The job has no schedule here. It starts only by hand or through the API. | Nothing, or add a schedule. |
| `no run principal` | It has a schedule, but no principal is bound. | Ask an administrator to bind one. |
| `run principal unusable` | The bound account is inactive or no longer a service account. | Ask an administrator to bind another. |

`lakana job show NAME` lists each trigger of one job on its own line. The [Jobs reference](../reference/jobs.md) has the fields. Under the table, `lakana job list` prints the sentence of each job that waits or cannot start.
["Why is it waiting?"](why-is-it-waiting.md#a-waiting-job) explains each one.

## Pause and resume

<!-- source: docs/examples/run-a-job/run.sh#pause -->
```bash
lakana job pause daily_silver --env dev
```

<!-- output -->
```text
Paused daily_silver in dev; resume it with 'lakana job resume daily_silver --env dev'.
```

<!-- source: docs/examples/run-a-job/run.sh#resume -->
```bash
lakana job resume daily_silver --env dev
```

<!-- output -->
```text
Resumed daily_silver in dev; next run 2026-10-15 06:00 Europe/Berlin (04:00 UTC).
```

- Whoever may **deploy** in that environment (the `DEPLOY` right, which is granted per
  environment) may pause and resume, for every repository there. Both are written to the audit log
  as `JOB_PAUSED` and `JOB_RESUMED`.
- A pause stops the schedule, not the job: you can still start it by hand, and a start that was
  already waiting still happens.
- A resume counts from now. The times that passed while the job was paused do not fire.
- A pause survives a deploy of the same job. A deploy that changes the cron expression recomputes
  the next time and keeps the pause.

## Start a job by hand or from CI

<!-- source: docs/examples/run-a-job/run.sh#run -->
```bash
lakana job run hourly_bronze --env dev
```

<!-- output -->
```text
154e7bca-6967-4645-a882-1817466b5730
bronze.readings  pending
bronze.readings  succeeded  attempt 1/3  1.0s
Run 154e7bca-6967-4645-a882-1817466b5730 succeeded
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━┳━━━━━━━┓
┃ asset           ┃ status    ┃ attempt ┃ duration ┃ rows ┃ error ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━╇━━━━━━━┩
│ bronze.readings │ succeeded │ 1/3     │ 1.0s     │ 6    │       │
└─────────────────┴───────────┴─────────┴──────────┴──────┴───────┘
```

`lakana job run NAME` follows the run like `lakana run` and exits 0 when it succeeds and 1 when it
fails, is cancelled or `--timeout` elapses; `--no-wait` prints the id and returns. It needs `RUN` on
the environment. The run carries the job, so it shows in `lakana runs show` and in the JSON of
`lakana runs list`:

<!-- source: docs/examples/run-a-job/run.sh#runs -->
```bash
lakana --output json runs list --env dev --limit 1
```

The fields `job`, `trigger` (`manual` for a person, `api` for a service account, `schedule` for the
timer, `deploy` for a start after a deploy; null for an ad-hoc `lakana run`), `scheduled_for` and `missed` are the last four of the run.

From CI, give the pipeline a service account with `RUN` on the environment and its token (see
[CI deploys to protected environments](projects.md#ci-deploys-to-protected-environments) for the
pattern with `DEPLOY`):

<!-- source: docs/examples/run-a-job/run.sh#ci-account -->
```bash
lakana principal create ci-jobs --type service-account
lakana token issue ci-jobs github-jobs
lakana grant create --principal ci-jobs --action RUN --kind ENVIRONMENT --env dev
```

`lakana token issue` shows the token once: store it as a CI secret. In the pipeline, with
`LAKANA_URL` and `LAKANA_TOKEN` set from the CI secrets:

<!-- source: docs/examples/run-a-job/run.sh#ci-run -->
```bash
lakana job run daily_silver --env dev --no-wait
```

<!-- output -->
```text
d47af24f-dea0-48d6-a742-8bf6120986a1
```

The run's `trigger` is `api` because the caller is a service account.

The same start through the REST API is `POST /api/v1/environments/{env}/jobs/{job}/runs`, with the
repository as for `POST /runs`. It answers:

| Answer | Meaning |
|---|---|
| `201` | The run is queued. The body is the run. |
| `202 Accepted` | The project is busy. The body has `waiting`: the start that waits and the run it waits for. |
| `404` | No such job. The message lists the jobs the repository has, or says it declares none. |

`lakana job run` prints what it waits for on a `202` and **exits 0**: that means accepted, not
run. A script that needs the result follows the run it names with `lakana runs show RUN_ID`.

A start by hand follows the rules of `lakana run` for who the run writes as: the bound run
principal if there is one; in an unprotected environment otherwise the caller; in a protected one
without a principal it is refused with a message that says to bind one.

## Start a job after each deploy

A job can also start by itself right after a deploy, for example to rebuild the tables whose SQL
the deploy just changed. Add `on_deploy: true` to it. This guide's second example,
`docs/examples/run-after-deploy/`, has the project:

<!-- source: docs/examples/run-after-deploy/project/jobs.yaml -->
```yaml
jobs:
  refresh:
    select: ["silver.*"]
    upstream: true
    on_deploy: true
environments:
  prod:
    jobs:
      refresh: { on_deploy: false }
```

`refresh` starts after each deploy in every environment but `prod`, where the override turns it
off. It has no schedule, so it starts only after a deploy, by hand or through the API; a job can
have both a schedule and `on_deploy: true`. A deploy prints the run it started:

<!-- source: docs/examples/run-after-deploy/run.sh#deploy -->
```bash
lakana deploy --env dev
```

<!-- output: run-after-deploy#deploy -->
```text
Queued deploy 0a45ad2b-cfcb-4ac2-8843-83cf57245e89
Deployed readings in dev at e34e3221afbf6ae591a6c39a1cffdbcf318ac207
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ change ┃ table                       ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ added  │ bronze.readings             │
│ added  │ silver.station_temperatures │
└────────┴─────────────────────────────┘
Jobs:
  added refresh
Jobs started after the deploy:
  refresh: run 3a17e4b3-b4ce-4b3f-b0f7-79fd38ac160b
Hint: follow a run with 'lakana runs show RUN_ID --env dev'.
```

The run has the trigger `deploy`, so you can find the runs that deploys started:

<!-- source: docs/examples/run-after-deploy/run.sh#runs -->
```bash
lakana --output json runs list --env dev --trigger deploy --limit 1
```

The rules:

- **It starts only when the deploy changes what is deployed.** That is, when the deploy makes
  another project version current: the first deploy, a new commit, or a commit you deployed
  before and deploy again over a newer one. Deploying the commit that is already current changes
  nothing and starts nothing. A dry run, a rollback and a deploy that fails start nothing either.
- **It runs as whoever deployed**, with the rights the deploy had, as if they had run
  `lakana job run` right after the deploy: they need `RUN` on the environment, and a protected
  environment needs a bound run principal (the run then writes as that account). A deploy through
  a CI token (`LAKANA_OIDC`) starts the jobs only when its CI trust rule opts in with
  `start_on_deploy_jobs`; the start is then allowed by the rule, and its audit event names it
  ([CI trust rules](../reference/ci-trust-rules.md)). The start is in
  the audit log as `RUN_STARTED` with the trigger `deploy` and the deployer as its actor.
- **A busy project makes it wait**, like any other start: the deploy prints
  `waits for run RUN_ID; it starts then`, and the job shows as `waiting` in `lakana job list`. When
  it begins it is checked again, as a waiting start by hand is.
- **A deployer without `RUN` still deploys.** The job does not start, and the deploy ends with a
  warning that names the job, the missing right and the two ways on. A CI account that may only
  deploy, for example:

<!-- source: docs/examples/run-after-deploy/run.sh#ci-account -->
```bash
lakana principal create ci-deploy --type service-account
lakana grant create --principal ci-deploy --action DEPLOY --kind ENVIRONMENT --env dev
```

Its deploy goes through, and says what to do about the job:

<!-- source: docs/examples/run-after-deploy/run.sh#ci-deploy -->
```bash
lakana deploy --env dev
```

<!-- output: run-after-deploy#ci-deploy -->
```text
Queued deploy c61d46b2-14af-42a5-9a9d-92b464e4041c
Deployed readings in dev at ffdc9d4c590f944b1351944e258e39839567f979
No changes.
warning: job `refresh` was not started: ci-deploy may not RUN in `dev`. Grant RUN with `lakana grant create --principal ci-deploy --action RUN --kind ENVIRONMENT --env dev`, or start the job yourself with `lakana job run refresh --repo readings --env dev`.
Hint: run 'lakana run --env dev' to run what you just deployed.
```

Grant the account `RUN` as the warning says, or start the job yourself. Every outcome is also in
the deploy's record, under `job_starts` in the JSON of the deploy
([Jobs reference](../reference/jobs.md#after-a-deploy)).

**A start can be lost.** The worker that ran the deploy starts the jobs right after the deploy
is recorded. If that worker stops in between (it crashed, or its machine went down), the deploy
stays succeeded and nothing tries again. The deploy's record then shows the job with the reason
`lost`, and `lakana deploy` prints a warning. The worker may have stopped after it started the
job and before it recorded that, so look for the job's run first, as the warning says
(`lakana runs list --trigger deploy --job JOB`), and start it by hand only if there is none.

## When the project is busy

Only one run of a project is active at a time. A time that comes, or a start by hand, while
another run is active does not fail:

- It **waits** and begins as soon as the project is free. `lakana job list` shows the job as
  `waiting`.
- **At most one start waits per job.** A later time, or a start by hand, merges into the waiting
  one; the run that begins says how many requests it stands for (`merged` in the audit event). If
  you start a job by hand while a scheduled start of it waits, the run that begins is the
  scheduled one: it writes as the bound service account and its trigger is `schedule`.
- A waiting start is **checked again when it begins**. If the person who asked was deactivated or
  may no longer `RUN` in the environment, or no run principal can be chosen any more, the start is
  dropped. The reason is recorded in the audit event `JOB_START_DROPPED`. It is not shown in
  `lakana job list`, so ask an administrator who can read the audit log (`lakana audit list`).
- When several jobs of a project wait, the one that has waited longest starts first.

## When the platform was down

A schedule is evaluated by the scheduler in the worker. If no worker ran at a time (the machine was
off, an upgrade took long), the time is not lost silently and not replayed either:

- When the scheduler is back, the job starts **once**, for the **latest** missed time. The run
  records the number of older times it stands in for in `missed`, and `scheduled_for` holds the
  time it is for. There is no catch-up.
- A time is **consumed when it is booked**, even if its start then fails. It does not come back.
  With no run principal bound, `lakana job list` shows `no run principal`. For any other failure
  (an inactive principal, the job removed, a selection that matches nothing, an error), the audit
  event `JOB_START_DROPPED` says why. A worker that stops in the instant between booking and
  starting loses that one time without an event.
- A pause that lands just after a time was booked does not stop that one start.

## Cron and time zones

The `cron:` text has the five standard fields: minute, hour, day of month, month, day of week.

- **Quote it in YAML.** A value that contains `*` or starts with `@` is a YAML error without
  quotes: `cron: "0 6 * * *"`, `cron: "@daily"`.
- **Day of week:** 0 and 7 are both Sunday. Names (`mon-fri`) work in the month and weekday
  fields.
- **A step runs to the end of the field:** `5/20` in the minute field means 5, 25, 45.
- **The two day fields.** When both day of month and day of week are restricted (neither starts
  with `*`), a day matches if **either** matches: `0 6 1 * mon` fires on the 1st of every month and
  on every Monday. When one of them starts with `*`, only the other counts. That includes a step
  over a star: `0 6 */2 * mon` fires **only** on Mondays, because `*/2` counts as a star here. This
  is how classic cron behaves.
- **A cron that can never fire is refused** at deploy, for example `0 0 31 2 *` (no 31 February).
  The message names the field to change.
- **Only IANA zone names** (`Europe/Berlin`, `America/New_York`, `UTC`). The `tzdata`
  package that ships with Lakana decides which names exist (the system's own database is not
  used), and names are case-sensitive: `Europe/Berlin`, not `europe/berlin`. A name that works on
  your laptop can be unknown to Lakana: the deploy says so and names the zone.

### Daylight saving time

What a schedule does when the clocks change depends on its hour field.

| The hour field | Follows | A time that does not exist (clocks go forward) | A time that happens twice (clocks go back) |
|---|---|---|---|
| A fixed hour: a number, a list, a range. `0-23` counts as fixed. | The wall clock | Fires at the first valid moment after it | Fires **once**, at its first occurrence |
| `*`, or a step over it (`*/2`) | Real time, as classic cron does | Nothing is shifted out of the gap | Fires in every real hour, **both occurrences** of the repeated hour |

So `30 2 * * *` in `Europe/Berlin` fires at 03:00 on the day clocks go forward (02:30 does not
exist) and once on the day they go back, while `15 * * * *` fires in every real hour of both days.
Pick a fixed hour for a daily job, and a star for a job that must not skip an hour.
