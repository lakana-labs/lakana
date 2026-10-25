# Train a model and score with it

At the end of this tutorial you will have a model stored as an **artifact**, a table of
predictions made with that model, and you will know how to look at the model and download it. An
artifact is a file that Lakana stores for you and numbers: every time you store it again, you get a
new **version**.

You build a project of three files. The first holds four customers: how often each visited a shop
and how much each spent. The second **trains** a model with
[scikit-learn](https://scikit-learn.org/), a straight line through those numbers, and stores it.
The third reads the model and **predicts** what each customer spends. At the end, a schedule runs
the project by itself.

You need what "Getting started" gave you: a Lakana installed on your machine, `git` set up with
your name, and [`uv`](https://docs.astral.sh/uv/) on your path. Plan on fifteen minutes. The first
run is slower than the next ones, because Lakana prepares a Python environment for your project
and downloads scikit-learn and the packages it needs, about 60 MB.

## Start a local instance

If the instance from [Getting started](getting-started.md) is still running, skip this section.
If not, start one in a terminal and leave that terminal open. Use a second terminal for everything
below. "Getting started" explains what you will see.

<!-- source: docs/examples/getting-started/start.sh#start -->
```bash
lakana server start --local
```

## Start a project

Create a new project. The name is `forecasts`, so that it does not clash with the `analytics`
project of "Getting started". The option `--no-sample` leaves out the sample that `lakana init`
writes, so the project holds only what you write:

<!-- source: docs/examples/train-and-predict/run.sh#init -->
```bash
cd ~
lakana init forecasts --no-sample
cd forecasts
```

`lakana init` prints a list of next steps, as in "Getting started". This tutorial gives its own
steps, so you can ignore that list.

## The training data

An **asset** is one file that describes one table or one artifact. Its place decides its name:
`assets/bronze/visits.sql` becomes the table `bronze.visits`. Create that file with this content.
It holds the four customers, written by hand, so that you need no data file:

<!-- source: docs/examples/train-and-predict/project/assets/bronze/visits.sql -->
```sql
/* lakana
description: How often four customers visited, and what each spent, written by hand.
columns:
  customer_id: The customer.
  visits: How many times the customer visited.
  spend: What the customer spent, in euros.
*/
SELECT customer_id, visits, spend
FROM (
  VALUES (1, 2, 12.0), (2, 4, 22.0), (3, 6, 32.0), (4, 8, 42.0)
) AS t(customer_id, visits, spend)
```

The comment at the top is the asset's documentation, as in "Getting started". DuckDB gives the
numbers in `12.0` an exact decimal type. You need not care: a Python asset receives such a column
as floating-point numbers, which is what scikit-learn expects. An asset that needs
the exact values asks for them with `@asset(decimals="exact")`; see
[Python assets](../how-to/projects.md#python-assets).

## The model asset

Create the folder `assets/models` and the file `assets/models/spend_model.py`. This asset trains
the model. The line it fits is `spend = slope * visits + intercept`, and scikit-learn's
`LinearRegression` finds the two numbers. `polars` is a library for tables: `pl.DataFrame` is a
table in memory, and Lakana gives your function the table `bronze.visits` in that form. `Path` is
Python's type for the name of a file; the function returns one:

<!-- source: docs/examples/train-and-predict/project/assets/models/spend_model.py -->
```python
from pathlib import Path

import joblib
import polars as pl
from sklearn.linear_model import LinearRegression

import lakana
from lakana import asset


@asset(inputs={"visits": "bronze.visits"}, output="artifact", keep_versions=2)
def spend_model(visits: pl.DataFrame) -> Path:
    """A straight line through spend by number of visits."""
    model = LinearRegression()
    model.fit(visits.select("visits").to_numpy(), visits["spend"].to_numpy())
    path = lakana.output_dir() / "model.joblib"
    joblib.dump(model, path)
    return path
```

Four things to know:

- `output="artifact"` says that this asset stores a file, not a table. The file's place gives the
  artifact its name: `models.spend_model`.
- `lakana.output_dir()` is an empty folder that belongs to this run. Write the file there.
- The function returns the path of the file it wrote. Lakana stores that file as it is, and
  numbers it as a new version. Here the file is the scikit-learn model, saved with `joblib`. Such a
  file is a program: loading it runs what is in it, so load only files you trust. See
  [A pickled model is code](../explanation/artifacts.md#a-pickled-model-is-code).
- `keep_versions=2` keeps the two newest versions. You will see what that does at the end.

## The scoring asset

Create the folder `assets/gold` and the file `assets/gold/spend_predictions.py`. It reads the model and the visits, and returns
a table:

<!-- source: docs/examples/train-and-predict/project/assets/gold/spend_predictions.py -->
```python
from pathlib import Path

import joblib
import polars as pl

from lakana import asset


@asset(inputs={"model": "models.spend_model", "visits": "bronze.visits"})
def spend_predictions(model: Path, visits: pl.DataFrame) -> pl.DataFrame:
    """What the model expects each customer to spend."""
    fitted = joblib.load(model)
    spend = fitted.predict(visits.select("visits").to_numpy())
    return visits.select("customer_id").with_columns(predicted_spend=pl.Series(spend))
```

The parameter `model` is a `Path`, not a table, because `models.spend_model` is an artifact. It
points to a copy of the model file that only this task can read. `visits` is a table, so it is a
`polars.DataFrame`, as in the model asset. `joblib.load` gives back the model that the first asset saved. Lakana sees from `inputs` that the model must exist
before the predictions, so you never state the order.

## Dependencies

A Python asset runs in its own Python environment. Lakana builds it from the project's lock file,
`uv.lock`. The environment needs `lakana[assets]`, which brings what a Python asset needs, among
it `polars`. The model asset also needs `scikit-learn`, which comes with the numerical packages it uses (`numpy`, `scipy` and `joblib`).

`lakana` comes from PyPI, like every other package, so both `uv lock` and the first run of your
project need a network connection to the public package index. Without one, `uv lock` fails, and
so does the worker when it builds your project for the first time. Behind a proxy, set
`HTTPS_PROXY` in the terminal before you run `uv lock`, and `LAKANA_RUN__HTTPS_PROXY` for the
worker.

Now go back to the project. Open `pyproject.toml`, which `lakana init` wrote. Delete the line
`dependencies = []`. In the comment block below it, delete the `# ` at the start of the
`dependencies` line. The comment lines above it can stay. The file then reads (the comments are
left out here):

<!-- source: docs/examples/train-and-predict/project/pyproject.toml -->
```toml
[project]
name = "forecasts"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["lakana[assets]==<version>", "scikit-learn>=1.5,<2"]

[dependency-groups]
dev = ["pytest>=8"]
```

The second entry of `dependencies`, `scikit-learn`, is the library that fits the line. The
number after `==` after `lakana[assets]` is the version of your instance; `lakana init` wrote the
version of the CLI that ran it, and the two are the same when you installed both together. Here
the number is shown as `<version>`. The `dev` group stays; it is for your own tests and is not
installed for a task.

Create the lock file. Run this in the terminal where you ran `source .venv/bin/activate`, in
`~/forecasts`:

<!-- source: docs/examples/train-and-predict/run.sh#lock -->
```bash
uv lock
```

It prints a few lines, ending with the number of packages, and creates the file `uv.lock`. The path
and the Python version on the first line are your own, and the number of packages changes with the packages on the public
index:

<!-- output: train-and-predict#lock -->
```text
Using CPython 3.14.4 interpreter at: /home/you/lakana/.venv/bin/python3
Resolved 35 packages in 327ms
```

`uv.lock` names every package, `lakana` among them, with the public index as its source. You
commit it with the rest.

## Commit, deploy and run

Git needs to know who you are. Skip this block if `git commit` has worked on this machine before,
as it did if you finished "Getting started". If not, set your name and email once, with your own
values:

<!-- source: docs/examples/train-and-predict/run.sh#git-identity -->
```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Turn the folder into a git repository and commit everything, including `uv.lock`. A deploy reads a
commit, so a file that is not committed is not deployed:

<!-- source: docs/examples/train-and-predict/run.sh#commit -->
```bash
git init
git add -A
git commit -m "A model and its predictions"
```

Tell Lakana where the repository is, and deploy it to `dev`:

<!-- source: docs/examples/train-and-predict/run.sh#deploy -->
```bash
lakana repo add forecasts --kind local --url "$PWD"
lakana deploy --env dev
```

You will see `Registered repository forecasts`, then the deploy. It lists all three assets as
added. The identifier and the commit differ on your machine:

<!-- output: train-and-predict#deploy -->
```text
Registered repository forecasts
Queued deploy e8da041b-d090-49d8-9d8e-731f4b10d4bf
Deployed forecasts in dev at ee370a0175331e6fdc657ceda4e5b5b8ebeeca69
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ change ┃ table                  ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━┩
│ added  │ bronze.visits          │
│ added  │ gold.spend_predictions │
│ added  │ models.spend_model     │
└────────┴────────────────────────┘
Hint: run 'lakana run --env dev' to run what you just deployed.
```

The column is called `table`, and the artifact `models.spend_model` is in it as well. Nothing is
built yet. Now run the project:

<!-- source: docs/examples/train-and-predict/run.sh#run -->
```bash
lakana run --env dev
```

Wait for it. The first run builds the Python environment, which can take a minute or more, and
the command prints a line each time an asset changes state. It ends with a summary like this one.
The run identifier and the durations differ on your machine:

<!-- output -->
```text
Run 8f620b06-184b-4fd0-8995-9b3a940c3f05 succeeded
┏━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━┳━━━━━━━┓
┃ asset                  ┃ status    ┃ attempt ┃ duration ┃ rows ┃ error ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━╇━━━━━━━┩
│ bronze.visits          │ succeeded │ 1/3     │ 1.0s     │ 4    │       │
│ gold.spend_predictions │ succeeded │ 1/3     │ 1.0s     │ 4    │       │
│ models.spend_model     │ succeeded │ 1/3     │ 1.0s     │      │       │
└────────────────────────┴───────────┴─────────┴──────────┴──────┴───────┘
```

All three succeeded. The model has no number in the `rows` column, because a model is a file, not
a table. Lakana ran `models.spend_model` first and used what it stored to build
`gold.spend_predictions`.

## Read the predictions

<!-- source: docs/examples/train-and-predict/run.sh#query -->
```bash
lakana sql "SELECT customer_id, predicted_spend FROM gold.spend_predictions ORDER BY customer_id"
```

You will see this:

<!-- output: train-and-predict#query -->
```text
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ customer_id ┃ predicted_spend ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ 1           │ 12.0            │
│ 2           │ 22.0            │
│ 3           │ 32.0            │
│ 4           │ 42.0            │
└─────────────┴─────────────────┘
4 rows
```

The four points lie exactly on one line, so the model reproduces them. scikit-learn calculates with
floating-point numbers, so the last digits can differ from one machine to the next: you may see
`12.000000000000005` where this page shows `12.0`. The line has a slope of 5 and an intercept of 2: a
customer is expected to spend 2 euros plus 5 euros for each visit. In your own projects, the
predictions are for customers the model has not seen.

## Look at the model

The model is not a table, so you cannot query it. List the artifacts instead:

<!-- source: docs/examples/train-and-predict/run.sh#artifact-list -->
```bash
lakana artifact list --env dev
```

You will see one row for the model. The `size` is that of its live versions:

<!-- output: train-and-predict#artifact-list -->
```text
┏━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━┓
┃ name        ┃ namespace ┃ current_version ┃ versions ┃ size  ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━┩
│ spend_model │ models    │ 1               │ 1        │ 639 B │
└─────────────┴───────────┴─────────────────┴──────────┴───────┘
```

To get the file, download the current version into a folder that does not exist yet. The second
command lists the folder:

<!-- source: docs/examples/train-and-predict/run.sh#artifact-download -->
```bash
lakana artifact download models.spend_model ~/spend-model --env dev
ls ~/spend-model
```

The first line names the file it made; your path is your own home folder. The second is the
folder's content. A `joblib` file is binary, so you cannot read it as text; load it with
`joblib.load` only if you trust it:

<!-- output: train-and-predict#artifact-download -->
```text
Downloaded dev/models.spend_model version 1 to /home/you/spend-model/model.joblib
model.joblib
```

The copy is yours alone: nobody else on the machine can read it. You can delete the folder
`~/spend-model` whenever you like; the stored model stays in Lakana.

## Run again

Run the project a second time:

<!-- source: docs/examples/train-and-predict/run.sh#second-run -->
```bash
lakana run --env dev
```

It takes less time than the first run. Every run trains the model again and stores it as a new
version, and the scoring asset uses the version that was stored in the same run. See the versions:

<!-- source: docs/examples/train-and-predict/run.sh#artifact-versions -->
```bash
lakana artifact versions list models.spend_model --env dev
```

You will see two versions, the newest first. Your times and run identifiers differ. A time or a
run identifier is never cut. If your window is too narrow for every column, columns are left out,
least useful first (`file_count`, `size`, `run_id`, `deleted_at` and last `manifest_sha256`), and
a line below the table names them. In a window it tells you to widen it or use `--output json`;
the output here was made in a window of 120 characters and written to a file, where there is no
window to widen, so the line mentions only `--output json`:

<!-- output -->
```text
┏━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ version ┃ created_at                  ┃ size  ┃ manifest_sha256 ┃ run_id                               ┃ deleted_at ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ 2       │ 2026-10-15T04:29:51.269544Z │ 639 B │ 420bf73218ec    │ 5ffb69b9-7cea-49f8-a277-1d9fab9a5c09 │            │
│ 1       │ 2026-10-15T04:29:37.608004Z │ 639 B │ 420bf73218ec    │ 08e77e55-6054-40f4-a659-6d2992b8bb48 │            │
└─────────┴─────────────────────────────┴───────┴─────────────────┴──────────────────────────────────────┴────────────┘
Not shown: file_count. Use --output json to see it.
Total: 2 versions with bytes, 1.2 KiB
```

Each version names the run that stored it. Both have the same `manifest_sha256`, the start of a
fingerprint of the files. Here they are the same, because the training data did not change and
scikit-learn writes the same bytes for the same model. A library does not always do that, so do not
count on it. Two versions with the same fingerprint hold the same files.

Because of `keep_versions=2`, a third run stores version 3 and removes the **files** of version 1.
Its record stays in the list, marked in the column `deleted_at`, and its number is never used
again. You can try it. Run once more:

<!-- source: docs/examples/train-and-predict/run.sh#second-run -->
```bash
lakana run --env dev
```

Then list the versions again:

<!-- source: docs/examples/train-and-predict/run.sh#artifact-versions -->
```bash
lakana artifact versions list models.spend_model --env dev
```

You will see three versions, the first with a date in `deleted_at`.

## Run it on a schedule

So far you started every run yourself. A **job** is a named selection of assets with a schedule:
Lakana starts it when the schedule says so, with no one at the keyboard. This job builds
everything under `gold` and what `gold` needs, every minute. A real job runs every hour or every
night; a minute lets you watch it. Add this to the end of `lakana.yaml`:

<!-- source: docs/examples/train-and-predict/jobs.yaml -->
```yaml
jobs:
  refresh_forecasts:
    select: ["gold.*"]
    upstream: true
    schedule: { cron: "* * * * *" }
```

To see that the schedule, and not you, builds the table, add a fifth customer to the data first:

<!-- source: docs/examples/train-and-predict/run.sh#schedule-customer -->
```bash
sed -i 's/(4, 8, 42.0)/(4, 8, 42.0), (5, 10, 52.0)/' assets/bronze/visits.sql
```

Commit both changes and deploy. The deploy lists the job and warns that it does not run yet:

<!-- source: docs/examples/train-and-predict/run.sh#schedule-deploy -->
```bash
git add -A
git commit -m "A fifth customer, and a job that refreshes the forecasts every minute"
lakana deploy --env dev
```

<!-- output -->
```text
[master 6f54bc9] A fifth customer, and a job that refreshes the forecasts every minute
 2 files changed, 6 insertions(+), 1 deletion(-)
Queued deploy a32ca74c-b18a-44c5-a14f-72acbce5f404
Deployed forecasts in dev at 6f54bc91be6d0cf8e5a730ba771d7c81dea6268c
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ change      ┃ table         ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ sql_changed │ bronze.visits │
└─────────────┴───────────────┘
Jobs:
  added refresh_forecasts
warning: job `refresh_forecasts` has a schedule but no run principal is bound in `dev`; it does not run until an admin binds one with `lakana deployments set-run-as`
Hint: run 'lakana run --env dev' to run what you just deployed.
```

A schedule needs somebody to run as: a **run principal**, which decides what the scheduled runs may
read and write. Without one the job does nothing, as the warning says. Create a service account,
give it the role of a data engineer in `dev`, and bind it to the deployment. The how-to
[Run a job on a schedule](../how-to/run-a-job-on-a-schedule.md) explains the choice:

<!-- source: docs/examples/train-and-predict/run.sh#schedule-bind -->
```bash
lakana principal create etl-dev --type service-account
lakana grant create --principal etl-dev --role DATA_ENGINEER --kind ENVIRONMENT --env dev
lakana deployments set-run-as forecasts --env dev --principal etl-dev
```

<!-- output: train-and-predict#schedule-bind -->
```text
Created service-account etl-dev
It can do nothing yet. Next:
  lakana grant create --principal etl-dev --role ANALYST --kind ENVIRONMENT --env ENV
  lakana token issue etl-dev NAME
Created grant 0fa07703-cece-4aa6-bb25-97ccb4d04896 (DATA_ENGINEER on environment dev) to etl-dev
┌────────────────────┬──────────────────────────────────────────┐
│ repository         │ forecasts                                │
│ project_version_id │ 105d369e-e764-46a1-bc36-c1f92894ef31     │
│ commit_sha         │ 6f54bc91be6d0cf8e5a730ba771d7c81dea6268c │
│ updated_at         │ 2026-10-15T04:37:30.964207+00:00         │
│ run_as             │ etl-dev                                  │
└────────────────────┴──────────────────────────────────────────┘
```

Now wait for the next full minute, and a few seconds more for the build. Ask for the newest run
until its `trigger` is `schedule`. Nobody ran `lakana run`; the schedule started it.

<!-- source: docs/examples/train-and-predict/run.sh#schedule-run -->
```bash
lakana --output json runs list --env dev --limit 1
```

<!-- output -->
```text
[
  {
    "id": "ce51e0da-ee47-4d4c-8e4e-392f87f6fc02",
    "environment": "dev",
    "repository": "forecasts",
    "project_version_id": "e9282730-766c-4c2e-9ebe-968e294fae4b",
    "commit_sha": "99c23734eb79415a86b8e6fb09357ebe174e283f",
    "kind": "selection",
    "status": "succeeded",
    "cancel_requested": false,
    "triggered_by": "etl-dev",
    "run_as": "etl-dev",
    "resumed_from_run_id": null,
    "selection": {
      "patterns": [
        "gold.*"
      ],
      "upstream": true,
      "downstream": false
    },
    "error_message": null,
    "created_at": "2026-10-15T04:55:00.145074+00:00",
    "started_at": "2026-10-15T04:55:01+00:00",
    "finished_at": "2026-10-15T04:55:09+00:00",
    "tasks": [],
    "job": "refresh_forecasts",
    "trigger": "schedule",
    "scheduled_for": "2026-10-15T04:55:00+00:00",
    "missed": 0,
    "notifications": []
  }
]
```

`job` names the job, `trigger` says that the schedule started it, and `scheduled_for` is the minute
it was due. A list leaves out the tasks; the `runs show` subcommand, given the run's `id`, prints
them in the table you know from `lakana run`, with `5` in the `rows` column of the two tables. The
run built all three assets from the commit you just deployed. The predictions now
cover five customers (and here the last digits show, as said above):

<!-- source: docs/examples/train-and-predict/run.sh#schedule-query -->
```bash
lakana sql "SELECT customer_id, predicted_spend FROM gold.spend_predictions ORDER BY customer_id"
```

<!-- output: train-and-predict#schedule-query -->
```text
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┓
┃ customer_id ┃ predicted_spend    ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━┩
│ 1           │ 12.000000000000005 │
│ 2           │ 22.000000000000004 │
│ 3           │ 32.0               │
│ 4           │ 42.0               │
│ 5           │ 52.0               │
└─────────────┴────────────────────┘
5 rows
```

The job keeps running every minute, and every run stores a new version of the model. Pause it when
you have seen enough, as in [Run a job on a schedule](../how-to/run-a-job-on-a-schedule.md):

<!-- source: docs/examples/train-and-predict/run.sh#schedule-pause -->
```bash
lakana job pause refresh_forecasts --env dev
```

<!-- output: train-and-predict#schedule-pause -->
```text
Paused refresh_forecasts in dev; resume it with 'lakana job resume refresh_forecasts --env dev'.
```

## What you built

You have a project that trains a model on every run, stores it as a numbered artifact and
predicts with it, and you listed the artifact, downloaded a version of it and watched the versions
grow. Everything stayed in files and commits: the model's versions say which run stored them, and
the lineage of the predictions shows that they come from the model:

<!-- source: docs/examples/train-and-predict/run.sh#lineage -->
```bash
lakana table lineage gold.spend_predictions --env dev
```

## Stop the instance

Go to the terminal where the instance runs and press Ctrl-C, or run `lakana server stop` from any terminal. The instance shuts down. Your tables
and your model stay where they are, and `lakana server start --local` brings the instance back with
them.

On your machine remain the folder `~/forecasts`,
the folder `~/spend-model` with the copy of the model that you downloaded, and the model itself in
Lakana. Delete the two folders when you no longer need them.

## Where next

- [Train a scikit-learn model and score with it](../how-to/models-with-scikit-learn.md) does the
  same in a project of your own, with a model of your own. A model saved by a library can run code when you load it, so read
  [A pickled model is code](../explanation/artifacts.md#a-pickled-model-is-code) first.
- [Limit how many versions are kept](../how-to/limit-artifact-versions.md) explains
  `keep_versions`.
- [Inspect and download artifact versions](../how-to/inspect-and-download-artifact-versions.md)
  shows every way to look at a version, and what to do when it fails.
- [Delete an artifact version](../how-to/delete-an-artifact-version.md) shows how to remove a
  version or the whole artifact.
- [Artifacts: what they are and what they are not](../explanation/artifacts.md) says why they
  work this way. Read its section
  [A pickled model is code](../explanation/artifacts.md#a-pickled-model-is-code) before you
  open a model file that somebody else made: loading a file saved with `pickle` or `joblib` can
  run any code in it. Read
  [a model reveals what it was trained on](../explanation/artifacts.md#access-is-all-or-nothing-and-a-model-reveals-what-it-was-trained-on)
  before you give somebody access to a model.
- [Artifacts, the reference](../reference/artifacts.md) lists every option, command and limit.
