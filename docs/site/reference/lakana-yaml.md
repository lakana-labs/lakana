# `lakana.yaml`

The project file at the root of the repository: the defaults, the requirements, the jobs and the
per-environment overrides. The steps that use it are in [Projects and deploys](../how-to/projects.md).


```yaml
repo: analytics              # lets the CLI omit the repository name
python: "3.12"               # the Python of your Python assets, see "The Python of your tasks"
defaults:                    # applied to every asset unless its front-matter overrides
  owner: data-eng
  materialize: table
  retries: 2                 # run policy, see "Retries and timeouts"
  retry_delay: 30s
  timeout: 10m
requirements:
  fields: [description, owner]   # each asset must have them, from front-matter or defaults
  column_docs: false             # true: every column of an explicit SELECT documented
properties:                  # free-form strings, stored for later milestones
  team_channel: "#data"
checks:                      # see "The default of the checks"
  on_failure: warn           # block or warn; each check may state its own
jobs:                        # see "Jobs"
  daily_gold:
    select: ["gold.*"]
    schedule: { cron: "0 6 * * *", timezone: Europe/Berlin }
    notify: { webhook: team-alerts, format: slack }   # see "Notify a webhook"
environments:                # per-environment overrides, deep-merged over the base
  prod:
    requirements: { column_docs: true }
    defaults: { retries: 4, timeout: 15m }
    checks: { on_failure: block }
  dev:
    jobs:
      daily_gold: { schedule: null }   # no schedule in dev
```

Unknown keys are errors. An override for an environment that does not exist is allowed.

## The Python of your tasks

`python:` chooses the Python your Python assets run on, when the worker runs tasks in containers.
Write it in quotes: YAML reads `3.10` as the
number 3.1.

```yaml
python: "3.12"
```

Without it, tasks run on the worker's own Python. It applies to every environment: an
`environments:` override cannot change it. `requires-python` in `pyproject.toml` must admit the
version's first release (`>=3.12`, or `==3.12.*`). A worker that runs tasks without containers
(`LAKANA_RUN__RUNNER=local`, local mode) refuses a project that names another Python than its own.

## `main_language` and `translations`

`main_language:` names the language your asset headers (descriptions, labels, columns) are
written in. `translations.require` lists languages in which every header text must also have a
translation. Both are optional.

```yaml
main_language: de          # the language of the asset headers
translations:
  require: [fr]            # optional: refuse a deploy with a gap in French
```

- Without `main_language:`, your texts are in the instance's main language, the server setting
  `LAKANA_MAIN_LANGUAGE` (default `en`).
- A project that ships a `translations/` folder or a non-empty `require` must declare
  `main_language:`. A later change of the server setting would otherwise silently relabel every
  header text.
- Both keys apply to the whole project: an `environments:` override cannot set them.
- `translations.require` is empty by default: a text without a translation shows in the main
  language. The files themselves, coverage and strict mode are described in
  [Translations](translations.md).
- A language is a tag of the form language, optional script, optional region (BCP 47), written
  `en`, `de`, `fr-CH` or `zh-Hant`: lower case for the language, Title case for the script, upper
  case for the region.

What a deploy says when the file is wrong:

| Mistake | Message |
|---|---|
| A tag that is no tag | ``main_language: `English` is not a language tag Lakana takes: a language, then optionally a script and a region (BCP 47), such as en, de, fr-CH or zh-Hant`` |
| A tag in the wrong case | ``main_language: `DE-ch` is written `de-CH`; use that spelling`` |
| A bad tag in `require` | the same two texts, with `translations.require.0:` as the field |
| A language listed twice | ``translations.require: lists `fr` twice`` |
| The main language in `require` | ``translations.require: `de` is the main language, whose texts are the asset headers; remove it from the list`` |
| `require` without `main_language:` | ``a project with translations must say which language its asset headers are written in; add `main_language:` with that language, such as `main_language: en`, to lakana.yaml`` |

## Jobs

A **job** is a name for a selection of assets, with an optional schedule, that can also start
after each deploy. It lives in `lakana.yaml`, so it is reviewed, versioned and rolled back with the
code, and a deploy swaps the jobs together with the assets.

A schedule starts the job at the times its `cron:` names, in its time zone. The scheduler checks
about once a second, so a run usually starts within a few seconds of the time; a busy scheduler or
a lock held by a deploy delays it to a later check. Four rules to know:

- **A scheduled run needs a run principal.** It runs as the service account bound to the
  deployment (`lakana deployments set-run-as`), never as a person. Without one nothing starts, the
  job shows the state `no run principal`, and the times that pass are not caught up: after you
  bind one, the next time fires, not the ones before it.
- **Only one run is active in a project.** A time that comes while another run is active waits
  and starts when that run ends. Only one start waits per job: later times merge into it.
- **A time is consumed even if its start fails.** When the bound principal is inactive or the
  job cannot start, the time does not come back; the audit event `JOB_START_DROPPED` says why, and
  `lakana job list` shows `run principal unusable` while the binding is bad. A pause that lands
  just after a time was taken does not stop that one run.
- **There is no catch-up.** After downtime (every worker stopped, for example) a job starts once,
  for the latest time it missed, and the run says how many earlier times it stands in for.

`lakana job list --env ENV` shows each job's state and next run; "Why is it waiting?" explains the
states. [Run a job on a schedule](../how-to/run-a-job-on-a-schedule.md) walks through it, and its section
[Cron and time zones](../how-to/run-a-job-on-a-schedule.md#cron-and-time-zones) has the rules for daylight
saving time and for the two day fields.

```yaml
jobs:
  daily_gold:
    select: ["gold.*"]           # shell globs over table names, as `lakana run --select`
    upstream: false              # as `lakana run --upstream`; default false
    downstream: false            # as `lakana run --downstream`; default false
    schedule:
      cron: "0 6 * * *"
      timezone: Europe/Berlin    # an IANA zone name; UTC when left out
    notify:
      webhook: team-alerts       # the name of a secret in the environment
      format: slack              # json (default), slack or teams
  hourly_silver:
    select: ["silver.*"]
    schedule: { cron: "15 * * * *" }
  by_hand:
    select: ["bronze.*"]         # no schedule: started by hand or through the API
  refresh:
    select: ["silver.*"]
    on_deploy: true              # also starts after each deploy; default false
environments:
  dev:
    jobs:
      daily_gold: { schedule: null, notify: null }   # no schedule and no notification in dev
  prod:
    jobs:
      refresh: { on_deploy: false }                  # not after a deploy in prod
```

- `jobs:` is optional. A job without `schedule:` has no timer.
- `select:` takes at least one pattern, with the meaning it has for `lakana run --select` (see
  [Selection patterns](../how-to/projects.md#selection-patterns)). A deploy refuses a pattern that matches no asset.
- `cron:` has exactly five fields: minute, hour, day of month, month and day of week. Each is `*`,
  a number, a range `a-b` or a comma list of these, with an optional step such as `*/15`. Months and
  weekdays may be three-letter names (`jan`, `mon-fri`). Weekdays count from 0 (Sunday) to 7, so 0
  and 7 are both Sunday and `fri-sun` is the same as `5-7`. The shorthands `@hourly`, `@daily`,
  `@weekly`, `@monthly` and `@yearly` are accepted. A sixth field for seconds is refused: a
  schedule fires at most once a minute. A deploy also refuses an expression that can never fire,
  such as `0 0 31 2 *` (there is no 31 February): change the day-of-month or the month field.
- Quote `cron:` in YAML. A value that contains `*` or starts with `@` is a YAML error unless it is
  in quotes: write `cron: "0 6 * * *"` and `cron: "@daily"`.
- `timezone:` is an IANA name such as `Europe/Berlin` or `America/New_York`. `UTC` is the default.
- `on_deploy:` is `true` or `false` (the default). With `true` the job starts after each deploy that
  makes another project version current in the environment (a new commit, or an earlier one
  deployed again over a newer one), as whoever deployed, who needs `RUN` there. A deploy of the
  commit already current, a dry run, a rollback and a failed deploy start nothing, and so does a
  deploy through a CI token whose trust rule does not opt in (`start_on_deploy_jobs`). [Start a job after each deploy](../how-to/run-a-job-on-a-schedule.md#start-a-job-after-each-deploy)
  has the rest.
- A job name follows the rules of a segment of an asset name: no dot, no slash, not empty.
- An environment can change a job the file declares, with the same keys (`on_deploy` too); the rest
  of the job is kept, and `schedule: null` or `notify: null` removes its schedule or its
  notification there.
  It cannot add a job.

Every problem comes back with the file, the line, the cause and the next step, for example
``lakana.yaml:5: jobs.daily_gold.schedule.cron: a cron expression has 5 fields (minute hour
day-of-month month day-of-week), got 6. A cron here has no seconds field: a schedule fires at most
once a minute. Example: '0 6 * * *' is every day at 06:00``, or
``lakana.yaml:3: job 'daily_gold': select 'platin.*' matches no asset. Patterns are shell globs
over table names such as 'gold.*'; change `select:` or add the asset.`` The selections are checked
against the assets when you deploy the environment they belong to.

A project version that declares jobs has manifest version 5; one in which a job has `notify:` has
version 6, so that a worker of an earlier release refuses a project version whose notifications it
would never send.

### Notify a webhook

`notify:` names where a job reports a run that failed or recovered, or that removed or retyped a
column ([the events](../how-to/get-told-when-a-job-fails.md#when-a-message-is-sent)).
It is optional; a job without it tells nobody.

- `webhook:` is the **name of a secret** of the environment, never the URL: the URL is a
  credential, so it stays out of the repository. A name has 1 to 255 characters and no space,
  control character or `:`.
- The secret is a JSON object with the URL under `url` and, to sign the `json` format, a
  `signing_key`. Create it in each environment that should notify (see
  [Manage secrets](../how-to/manage-secrets.md)):

<!-- source: docs/examples/get-told/run.sh#secret -->
```bash
printf '{"url": "https://hooks.example.com/services/T000/B000/XXXX"}' \
  | lakana secret create team-alerts --env dev --values-stdin
```

- `format:` is `json` (the default), `slack` or `teams`.
- `notify: null` under `environments:` turns the notification off in that environment; another
  `webhook:` or `format:` there changes only that key.
- A deploy refuses an unknown `format` and a `webhook` that is not a valid secret name, with the
  file, the line and the next step. It does **not** refuse a name with no secret: the deploy goes
  through and prints a warning that names the job, the secret, the environment and the command
  that creates it. A revoked secret gets its own warning: its name cannot be reused, so the warning
asks for a secret under another name in `notify.webhook`. A dry run words it as "would not notify".
  Only the name is looked up; the secret is not read.
- **Test it before a run fails.** `lakana job notify-test daily --env prod` sends a message with
  clearly fake run data to the job's webhook and waits up to 30 seconds. It prints `delivered`,
  or `failed:` with the reason (for example that the address is private, and how to allow it), or
  `still trying (attempt 2 of 5)` with the command that shows the result later. It needs the
  deploy right in the environment. A job without `notify:` in that environment is refused with
  the next step.
- `lakana runs show RUN --env prod` lists the notifications of a run with their state:
  `delivered`, `retrying (attempt 2 of 5)` or `failed: the server answered 404`. The guide
  [Get told when a job fails](../how-to/get-told-when-a-job-fails.md) shows the formats, the signature and
  every reason.

## The default of the checks

`checks.on_failure` sets what a check does when it fails, for every check of the project that
does not state `on_failure` in its own `/* lakana */` comment. It takes `block` or `warn`. The
order is the check's own value, then this one, then `block`. Use `warn` while you add checks to a
pipeline that already runs, so that a check you got wrong does not stop it, and `block` for an
environment that must not publish bad data; the example above warns everywhere except in `prod`.
A deploy and a dry run tell you when the default applies, with a line after the list of checks, for example
``2 checks take `on_failure: warn` from lakana.yaml.`` `keep` is refused as a
default (``checks.on_failure: must be block or warn (got 'keep'). `keep` leaves failing data
where readers find it, so it can only be set on a check: put `on_failure: keep` in that check's
/* lakana */ block.``): it leaves failing data where readers find it, so you choose it check by
check. [Check your data](../how-to/check-your-data.md#adding-checks-to-a-pipeline-that-already-runs) walks
through the order in which to turn checks on.

## Retries and timeouts

Both `defaults:` in `lakana.yaml` (and its per-environment overrides) and an asset's front-matter
accept three run settings:

```yaml
retries: 2          # retries after a failed attempt (default 2)
retry_delay: 30s    # first retry delay, doubled per attempt ±20 %, at most 10 minutes (default 30s)
timeout: 15m        # wall-clock limit of one attempt (default: the operator maximum)
```

A duration is a whole number of seconds or `<n>h`, `<n>m`, `<n>s` parts in that order (`90s`,
`5m`, `1h30m`). They are resolved at deploy time and stored in the project version, so a scheduled run
needs no flags. The operator caps them: `LAKANA_RUN__MAX_RETRIES` (default 5) and
`LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS`; a deploy that asks for more fails validation, naming the file.
Lowering a cap later applies to the next run. A task's query that passes its limit is stopped for
good: it gets `LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS` (10 s) to stop by itself.

Independent of the operator caps, `retries` is at most 10,000 and `retry_delay` and `timeout` are
at most one year (31,536,000 s); a larger value fails validation.

For example, with the `lakana.yaml` above an asset without run settings gets 2 retries, 30 s then
60 s apart (±20 %), and 10 minutes per attempt in `dev`; in `prod` 4 retries and 15 minutes
(the default operator maximum). An asset
that calls a flaky upstream can override just what it needs in its front-matter:

<!-- not tested: SQL is not parsed by the block checker; a test in `scripts/tests/test_docs_site.py` parses this header with the front-matter reader -->
```sql
/* lakana
retries: 5
retry_delay: 2m     # 2 m, 4 m, 8 m, then capped at 10 m
*/
SELECT * FROM bronze.partner_feed
```

`retries: 0` disables retries; a timeout, a lost worker and storage or network errors are
retried, validation errors and permission refusals never are.
