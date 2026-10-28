# Build a table with Python

At the end of this tutorial you will have a project with SQL assets and one **Python asset**, and
a table that the Python asset built from two of the SQL tables. You write no code: `lakana init`
writes the project for you, and you deploy it, run it and query the result.

The project is the sample of [Getting started](getting-started.md), with one file more. The
sample generates temperature readings from three weather stations and builds two tables from them
with SQL. The Python asset reads the readings and each station's average, and says for every
reading how much warmer or colder than its station's average it was.

You need what "Getting started" gave you: a Lakana installed on your machine and `git` set up with
your name. You also need [`uv`](https://docs.astral.sh/uv/) on your path and a network connection
to the public package index, PyPI. Plan on fifteen minutes. This tutorial takes a few minutes
longer than "Getting started", because Lakana prepares a Python environment for the Python asset:
its packages are resolved and pinned when you create the project, and downloaded when it first
runs.

## Start a local instance

If the instance from [Getting started](getting-started.md) is still running, skip this section.
If not, start one in a terminal and leave that terminal open. Use a second terminal for everything
below.

<!-- source: docs/examples/getting-started/start.sh#start -->
```bash
lakana server start --local
```

## Tell git who you are

Skip this block if `git commit` has worked on this machine before, as it did if you finished
"Getting started". If not, set your name and email once, with your own values:

<!-- source: docs/examples/python-sample/run.sh#git-identity -->
```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

## Create the project

Go to your home folder and create the project `weather` with the template `python`:

<!-- source: docs/examples/python-sample/run.sh#init -->
```bash
cd ~
lakana init weather --template python
cd weather
```

`lakana init` first says that it needs the network and a few minutes, and why. Then it writes the
project and runs `uv lock` in it. You do not run `uv lock` yourself. When the lock is done, it
lists what to do next. Your folder is your own. If you installed Lakana from a checkout, a note
about the development build comes last; with a release there is no such note:

<!-- output: python-sample#init -->
```text
The template python locks the project's Python packages with `uv lock` once it is written. That needs network access to the public package index and can take a few minutes: uv resolves every package the Python asset needs and pins it in uv.lock.
Created a Lakana project in /home/you/weather from the template python: the sample (generated readings from three weather stations, three SQL assets built from them, and one check) and a Python asset, assets/gold/temperature_anomalies.py, built from two of the sample's tables.
Locked its packages in uv.lock; commit it with the rest (README.md says why).
Next:
  cd /home/you/weather
  git init && git add -A && git commit -m 'Initial project'
  lakana repo add weather --kind local --url /home/you/weather
Then build the project. The first run also builds the Python asset's environment, which downloads its packages and takes a few minutes more:
  lakana deploy --env dev
  lakana run --env dev
Then look at the Python asset's table:
  lakana sql "SELECT * FROM gold.temperature_anomalies WHERE station_id = 'B2' ORDER BY day"
Local repositories must lie under LAKANA_REPO_ROOTS (local mode allows your home directory).
The CI files deploy without a stored Lakana token: the comment at the top of each says what to set up once (--ci-auth pat writes files that use a stored token).
Note: lakana 0.20.1.dev34+gf48033c4b is a development build, so the CI install `lakana==0.20.1.dev34+gf48033c4b` can't be installed from PyPI. Ignore this until you use CI; then edit the pinned version in .github/workflows/lakana-deploy.yml and .gitlab-ci.yml, or set LAKANA_INDEX_URL to an index that has it.
```

If `lakana init` stops with an error, or you press Ctrl-C while it locks, it leaves nothing
behind, so you can run the same command again once the cause is fixed. The error says what went wrong:

- `uv` is not on your path: install it, as the error says.
- `uv lock` failed: most often there is no connection to the public package index. Behind a
  proxy, set `HTTPS_PROXY` in the terminal first. To try Lakana without the network, leave out
  `--template python`: the sample of "Getting started" needs no download.

## What `lakana init` wrote

The folder holds the sample of "Getting started": `assets/bronze/readings.sql` generates the
readings, `assets/silver/station_temperatures.sql` and `assets/gold/station_ranking.sql` build
two tables from them, and a check under `checks/` looks at the averages. "Getting started"
explains each of these files. It also holds a `README.md` with the steps of this page.

The Python asset is `assets/gold/temperature_anomalies.py`. Its place makes it the table
`gold.temperature_anomalies`:

<!-- source: docs/examples/python-sample/project/assets/gold/temperature_anomalies.py -->
```python
"""How far each reading lies from its station's average: a Python asset built from the sample."""

import polars as pl

from lakana import asset


@asset(
    inputs={"readings": "bronze.readings", "stations": "silver.station_temperatures"},
    columns={
        "station_id": "The station that took the reading.",
        "day": "The day of the reading.",
        "temp_c": "The temperature in degrees Celsius.",
        "anomaly_c": "How many degrees the reading lies above (positive) or below (negative) "
        "the station's average.",
    },
)
def temperature_anomalies(readings: pl.DataFrame, stations: pl.DataFrame) -> pl.DataFrame:
    """Each reading, and how much warmer or colder than its station's average it was."""
    averages = stations.select("station_id", "avg_temp_c")
    return (
        readings.join(averages, on="station_id")
        .with_columns(anomaly_c=pl.col("temp_c") - pl.col("avg_temp_c"))
        .select("station_id", "day", "temp_c", "anomaly_c")
        .sort("station_id", "day")
    )
```

Three things to know:

- `@asset` makes the function an asset. `inputs` names the tables it reads: Lakana passes the
  table `bronze.readings` as the parameter `readings`, and `silver.station_temperatures` as
  `stations`. From `inputs` Lakana also knows that both tables must be built first, so you never
  state the order.
- Each table arrives as a `polars.DataFrame`, a table in memory. [Polars](https://pola.rs/) is a
  library for tables, and Lakana brings it with every Python asset. The function joins the two
  tables on the station and subtracts the average from each temperature.
- The function returns a table, and Lakana stores it as `gold.temperature_anomalies`. The
  docstring and `columns` describe the table and its columns, as the comment at the top of a SQL
  asset does.

A Python asset runs in a Python environment of its own. Lakana builds it from two files that
`lakana init` wrote. `pyproject.toml` names the package the asset needs, `lakana[assets]`, at the
version of the CLI that ran `lakana init`. `uv.lock` pins that package and every package it needs,
each with its exact version and a hash. That is what `uv lock` did, and why it needed the network.
Lakana never resolves versions for you, so the environment is the same on every machine and on
every run. Commit `uv.lock` with the code. If you add a package to `pyproject.toml` later, run
`uv lock` again and commit both files.

## Commit and deploy

A deploy reads a commit, so a file that is not committed is not deployed. Turn the folder into a
git repository and commit everything, `uv.lock` included:

<!-- source: docs/examples/python-sample/run.sh#commit -->
```bash
git init --quiet
git add -A
git commit --quiet -m "Initial project"
```

Tell Lakana where the repository is, and deploy it to `dev`:

<!-- source: docs/examples/python-sample/run.sh#deploy -->
```bash
lakana repo add weather --kind local --url "$PWD"
lakana deploy --env dev
```

The deploy lists four tables as added, the Python asset's among them, and the check. The
identifier and the commit differ on your machine:

<!-- output: python-sample#deploy -->
```text
Registered repository weather
Queued deploy 751899f2-1545-45dd-b616-490da66749dd
Deployed weather in dev at d3c232a98ccb6d6f111e89cebbcdb2f86a54d486
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ change ┃ table                       ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ added  │ bronze.readings             │
│ added  │ gold.station_ranking        │
│ added  │ gold.temperature_anomalies  │
│ added  │ silver.station_temperatures │
└────────┴─────────────────────────────┘
Checks:
  silver.station_temperatures: plausible_range
Hint: run 'lakana run --env dev' to run what you just deployed.
```

## Run

<!-- source: docs/examples/python-sample/run.sh#run -->
```bash
lakana run --env dev
```

Wait for it. The command prints a line each time an asset changes state and ends with a summary
of the four assets and the check. The first run builds the Python asset's environment: it
downloads `lakana[assets]` and the packages it needs, which can take a few minutes. The next runs
reuse the environment. When the run is done, every asset says `succeeded`, and
`gold.temperature_anomalies` has 18 rows, one for each reading.

## Query the Python asset's table

Look at the readings of the station `B2`, the command that `lakana init` suggested:

<!-- source: docs/examples/python-sample/run.sh#query -->
```bash
lakana sql "SELECT * FROM gold.temperature_anomalies WHERE station_id = 'B2' ORDER BY day"
```

You will see this:

<!-- output: python-sample#query -->
```text
┏━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┓
┃ station_id ┃ day        ┃ temp_c ┃ anomaly_c ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━┩
│ B2         │ 2026-01-01 │ 0.0    │ -1.0      │
│ B2         │ 2026-01-02 │ 1.0    │ 0.0       │
│ B2         │ 2026-01-03 │ 2.0    │ 1.0       │
│ B2         │ 2026-01-04 │ 0.0    │ -1.0      │
│ B2         │ 2026-01-05 │ 1.0    │ 0.0       │
│ B2         │ 2026-01-06 │ 2.0    │ 1.0       │
└────────────┴────────────┴────────┴───────────┘
6 rows
```

The station's average is 1.0 degrees, so a reading of 0.0 lies one degree below it and a reading
of 2.0 one degree above.

## What you built

You have a project in which SQL builds tables and a Python asset builds a table from them, all
from one commit. The Python asset ran in an environment that `uv.lock` pins, so the next run, on
this machine or another, uses exactly the same packages.

## Stop the instance

Go to the terminal where the instance runs and press Ctrl-C, or run `lakana server stop` from any
terminal. Your tables stay where they are, and `lakana server start --local` brings the instance
back with them. Delete the folder `~/weather` when you no longer need it.

## Where next

- [Train a model and score with it](train-a-model.md) adds a package of your own,
  scikit-learn, to a project, trains a model and stores it as an artifact.
- [Python assets](../how-to/projects.md#python-assets) in "Projects and deploys" shows how to add a
  Python asset to a project of your own.
- [Python assets, the reference](../reference/python-assets.md) lists the rules of a Python asset,
  what a deploy checks and what a task may do.
- [Python assets: what they are for and how far they are trusted](../explanation/python-assets.md)
  says when to write a Python asset and when a SQL one.
