# Getting started

At the end of this tutorial you will have a Lakana instance running on your own machine, a table
loaded from a file, three tables built by a sample project you deployed, and one query answered
from those tables. You do not need
Docker or a database server.

Lakana is pre-beta: the API and the data layout may change between 0.x releases, so do not put
production data in it yet. It has no web interface yet: everything in this tutorial is done in a
terminal with the CLI, and the interface is planned before the public beta.

You need Python 3.11 or newer (on macOS: Python 3.13 or newer) and git. With an older Python on macOS, `lakana server start --local` stops before it starts anything and says so. Once Lakana is installed, plan on ten minutes.

## Install

Lakana is one Python package, published on PyPI as a pre-beta (0.x). The `server` extra adds what
a local instance needs:

<!-- not tested: installs from PyPI -->
```bash
pip install "lakana[server]"
```

To check that a download is the one Lakana published, see
[Verify a download](../how-to/deploy-with-compose.md#verify-a-download).

To work on Lakana itself, or to try the development version, install from a checkout of the
repository instead. You need [`uv`](https://docs.astral.sh/uv/), a Python package manager:

<!-- not tested: installs from a checkout -->
```bash
git clone https://github.com/lakana-labs/lakana.git
cd lakana
uv sync --all-packages
source .venv/bin/activate
```

The last line puts the `lakana` command on your path, for this terminal only. Run it again in
every new terminal you open below, from inside the checkout. A checkout is a development build:
at the end of its output, `lakana init` prints a note about pinned versions for CI. You can
ignore it in this tutorial.

## Start a local instance

A **local instance** is a complete Lakana on your machine: the server, a worker that runs your
queries and builds, and a place to keep tables. Start it:

<!-- source: docs/examples/getting-started/start.sh#start -->
```bash
lakana server start --local
```

The instance stays in the foreground and keeps this terminal. It prints no line per request, so
the line that says it is ready is the last one you see. When it is ready, it prints a
line like this one:

<!-- output -->
```text
Lakana is running at http://127.0.0.1:8765 — profile 'local', environment 'dev'. Try: lakana sql "SELECT 42"
```

To see a line for every request the server answers, start with the option `--log-requests`. To
see more or fewer of the other lines, set `LAKANA_LOG__LEVEL` (for example to `DEBUG`) in front of
the command; local mode passes it to the server and the worker.

The worker has a memory limit, 4 GB by default (`LAKANA_WORKER__MEMORY_LIMIT`; `off` turns it
off). It covers the worker and the tasks and queries it started. On Linux, a worker that goes over it ends the
largest running query or Python task first (that one fails, the worker and the rest go on); if the
worker is still over the limit after that, it stops and is started again (local mode does that, at most three times in five minutes, and prints one line), and the query or task that was running is marked failed. The worker says which limit applies
in one line at start. On Linux it measures the worker and its tasks; on macOS only the worker
process, and on Windows nothing, and then the line says the limit is not enforced. You can ignore
that line. In local mode a very fast query can still use up the machine's memory before the worker
notices; the Compose bundle adds a limit that the kernel enforces.

Leave this terminal as it is. **Open a second terminal** for the rest of the tutorial, and
activate the environment there too if you installed from a checkout.

To see that it worked, ask for the version. The command prints the version of the `lakana`
command and, because an instance is running, the version of the instance:

<!-- source: docs/examples/getting-started/run.sh#version -->
```bash
lakana --version
```

The versions, and the port in the second line, differ on your machine:

<!-- output: getting-started#version -->
```text
lakana 0.8.1.dev6+g6af3c5d2a
instance http://127.0.0.1:8765: 0.8.1.dev6+g6af3c5d2a
```

!!! note
    If the start fails with "Port 8765 is in use.", another program uses that port. Stop that
    program and start again, or pass another port with `--port`; the commands below then find
    your instance on it. If it fails with "Lakana is already running", an instance you
    started earlier still runs; its message names the address. Use it, or stop it with
    `lakana server stop` (add `--data-dir` if you started it with one) and start again.

An **environment** is a separate place where a project is deployed and its tables live. A local
instance has one, called `dev`. Every command below that needs an environment names it with
`--env dev`.

## Load a file

The quickest way to see your own data in Lakana is to load a file. Save these lines as
`readings.csv` in the folder your second terminal is in:

<!-- source: docs/examples/getting-started/readings.csv -->
```text
station_id,day,temp_c
A1,2026-01-07,3.5
B2,2026-01-07,0.5
C3,2026-01-07,8.0
```

Then load it:

<!-- source: docs/examples/getting-started/run.sh#load -->
```bash
lakana load readings.csv first_file --env dev --yes
```

The command reads the file, shows each column with the type it found, and writes the rows into
a new table, `bronze.first_file`:

<!-- output: getting-started#load -->
```text
readings.csv: 3 rows, 3 columns
  station_id: string
  day: date32[day]
  temp_c: double
Loaded 3 rows into dev/bronze.first_file (created the table). To clean it up or load it again on a schedule, write an asset that reads it.
```

Without `--yes`, it asks before it writes. A load always lands in the **bronze** layer, the
tables that hold data as it comes in (the sections below say more about layers): a bare name
such as `first_file` means `bronze.first_file`, and any other layer is refused. It reads CSV,
Parquet, Arrow and newline-delimited JSON files; a JSON array and spreadsheets need the `assets`
extra of the `lakana` package as well. Loading the file again replaces the table's rows; with
`--write append` it adds them.

`lakana load` is for exploring, testing and teaching. A table you clean up, and anything that
should run again, belongs in an asset, which the rest of this tutorial builds; a
[Python asset](../reference/python-assets.md) can read files. The
[load reference](../reference/load.md) has every option and error.

## Create a project

A **project** is a git repository of SQL and Python files. Lakana reads it from git, so every
table you build can be traced to a commit.

A local instance only accepts projects under your home directory. The first command goes
there, so that you do not create the project in `/tmp`. (A project elsewhere is refused when you
register it, with a message that tells you to move it under your home directory.)

<!-- source: docs/examples/getting-started/run.sh#init -->
```bash
cd ~
lakana init analytics
cd analytics
```

`lakana init` writes a small sample, and lists what to do next. Yours shows your own folder, and
a note about the development build if you installed from a checkout:

<!-- output -->
```text
Created a Lakana project in /home/you/analytics, with a small sample: generated readings from three weather stations, three SQL assets built from them, and one check.
Next:
  cd /home/you/analytics
  git init && git add -A && git commit -m 'Initial project'
  lakana repo add analytics --kind local --url /home/you/analytics
Then the two commands that build the sample:
  lakana deploy --env dev
  lakana run --env dev
Then look at the result:
  lakana sql "SELECT * FROM gold.station_ranking ORDER BY warmth_rank"
Local repositories must lie under LAKANA_REPO_ROOTS (local mode allows your home directory).
If 'git commit' says it does not know who you are, set a name and email first: git config --global user.name 'Your Name' && git config --global user.email you@example.com
Note: lakana 0.8.1.dev6+g6af3c5d2a is a development build, so the CI install `lakana==0.8.1.dev6+g6af3c5d2a` can't be installed from the index. Ignore this until you use CI; then edit the pinned version in .github/workflows/lakana-deploy.yml and .gitlab-ci.yml.
```

You now have a folder `analytics` with a `lakana.yaml` file, which holds the project's settings.
`lakana init` also wrote files for CI and for Python assets; this tutorial leaves them as they
are. The rest of what it wrote is the sample, and the next sections read it, so that you know
what you are about to build. You do not need to write anything. (To start a project without the
sample, run `lakana init --no-sample`.)

## The sample

An **asset** is one file that describes one table. Lakana runs the file and stores the result as
the table. Its path decides the table's name: `assets/bronze/readings.sql` becomes the table
`bronze.readings`. Here `bronze` is the namespace, a folder of tables, and `readings` is the name.

Many teams sort tables into layers. **Bronze** tables hold data as it comes in. **Silver**
tables hold data that has been cleaned or summed up from bronze. **Gold** tables are the ones
people read. Lakana does not require this; the sample follows it because it is a common way to
keep order.

The first asset, `assets/bronze/readings.sql`, is the source. It holds eighteen temperature
readings from three weather stations. The query generates them, so you need no data file, and
the table is the same on every machine:

<!-- source: docs/examples/getting-started/project/assets/bronze/readings.sql -->
```sql
/* lakana
description: Eighteen generated readings from three weather stations, six days each.
columns:
  station_id: The station that took the reading.
  day: The day of the reading.
  temp_c: The temperature in degrees Celsius.
*/
SELECT
  stations.station_id,
  DATE '2026-01-01' + CAST(days.n AS INTEGER) AS day,
  CAST(stations.base_c + (days.n % 3) - 1 AS DOUBLE) AS temp_c
FROM (VALUES ('A1', 4.0), ('B2', 1.0), ('C3', 9.0)) AS stations(station_id, base_c)
CROSS JOIN (VALUES (0), (1), (2), (3), (4), (5)) AS days(n)
```

The comment at the top is the asset's documentation: what the table holds, and what each column
means. The project's settings require a description for every asset, and Lakana refuses a deploy
without one. The rest of the file is a single `SELECT` query.

The second asset, `assets/silver/station_temperatures.sql`, reads `bronze.readings` and sums it
up for each station:

<!-- source: docs/examples/getting-started/project/assets/silver/station_temperatures.sql -->
```sql
/* lakana
description: The lowest, highest and average temperature of each station.
columns:
  station_id: The station that took the readings.
  readings: How many readings the station took.
  min_temp_c: The lowest temperature in degrees Celsius.
  max_temp_c: The highest temperature in degrees Celsius.
  avg_temp_c: The average temperature in degrees Celsius.
*/
SELECT
  station_id,
  COUNT(*) AS readings,
  MIN(temp_c) AS min_temp_c,
  MAX(temp_c) AS max_temp_c,
  AVG(temp_c) AS avg_temp_c
FROM bronze.readings
GROUP BY station_id
```

The third, `assets/gold/station_ranking.sql`, reads the silver table and puts the stations in
order, from the warmest to the coldest:

<!-- source: docs/examples/getting-started/project/assets/gold/station_ranking.sql -->
```sql
/* lakana
description: The stations from the warmest to the coldest.
columns:
  station_id: The station.
  avg_temp_c: The station's average temperature in degrees Celsius.
  warmth_rank: 1 for the warmest station, 2 for the next, and so on.
*/
SELECT
  station_id,
  avg_temp_c,
  RANK() OVER (ORDER BY avg_temp_c DESC) AS warmth_rank
FROM silver.station_temperatures
```

Because each asset reads the table of the one before, Lakana knows the order in which to build
them. You never state the order yourself.

## The check

A **check** is a file that says what must be true of a table. It is one `SELECT` that returns the
rows that are wrong; a table with no wrong rows passes. Its path names the table it checks:
`checks/silver/station_temperatures/plausible_range.sql` is a check of `silver.station_temperatures`.
It looks for a station whose average temperature is impossible, which means a faulty sensor:

<!-- source: docs/examples/getting-started/project/checks/silver/station_temperatures/plausible_range.sql -->
```sql
/* lakana
description: An average below -90 or above 60 degrees is a sensor fault.
*/
SELECT station_id, avg_temp_c
FROM silver.station_temperatures
WHERE avg_temp_c < -90 OR avg_temp_c > 60
```

Every run tests a table after it builds it. If this check returned a row, the run would fail and
the table would be set back to what it was before. With your three stations it returns none. The
how-to guide [Check your data](../how-to/check-your-data.md) shows what a failing check looks like.

## Commit

Git can only commit if it knows who you are. If you have used git on this machine before, it
does, and you can skip to the next block. If not, `git commit` stops with a message that asks for
your name and email; `lakana init` printed a line about this too ("If 'git commit' says it does
not know who you are ..."). Set them once, with your own values:

<!-- source: docs/examples/getting-started/run.sh#git-identity -->
```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

Now turn the folder into a git repository and commit your files:

<!-- source: docs/examples/getting-started/run.sh#commit -->
```bash
git init
git add -A
git commit -m "Initial project"
```

A deploy reads a commit, not the files in your folder, so a change that is not committed is not
deployed. Git may also print a long hint about the name of the first branch. It is harmless.

## Deploy

To **deploy** is to give Lakana a commit of a project for one environment. Lakana checks the
project, and if the checks pass, records it as the version that runs there. The first command
tells Lakana where the repository is. The second deploys the latest commit to `dev`.

<!-- source: docs/examples/getting-started/run.sh#deploy -->
```bash
lakana repo add analytics --kind local --url "$PWD"
lakana deploy --env dev
```

You will see `Registered repository analytics`, then the deploy: its identifier, the commit it
deployed, a table of what changed and, last, a hint that names the next step. Yours looks like
this, with a different identifier and commit:

<!-- output -->
```text
Queued deploy 71ddefdc-6a57-4c5b-bac3-cbacc2fb4759
Deployed analytics in dev at ca36c93fe2337b324df3ab8d03cac9b9d1ed1420
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ change ┃ table                       ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ added  │ bronze.readings             │
│ added  │ gold.station_ranking        │
│ added  │ silver.station_temperatures │
└────────┴─────────────────────────────┘
Checks:
  silver.station_temperatures: plausible_range
Hint: run 'lakana run --env dev' to run what you just deployed.
```

All three tables are listed as added, but no table exists yet. A deploy only records what to
build. Under `Checks:` it lists the check it found, and the hint names the next step.

## Run

A **run** builds the tables of the deployed version, in the order their dependencies need. It
is the command the hint named:

<!-- source: docs/examples/getting-started/run.sh#run -->
```bash
lakana run --env dev
```

The command prints one line each time an asset changes state and waits until the run is done.
It ends with a summary like this one. The run identifier above it differs on your machine, and
so may the durations:

<!-- output -->
```text
Run e6c654de-926d-4941-896d-909ead7cd45d succeeded
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━┳━━━━━━━┓
┃ asset                       ┃ status    ┃ attempt ┃ duration ┃ rows ┃ error ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━╇━━━━━━━┩
│ bronze.readings             │ succeeded │ 1/3     │ 0.0s     │ 18   │       │
│ gold.station_ranking        │ succeeded │ 1/3     │ 1.0s     │ 3    │       │
│ silver.station_temperatures │ succeeded │ 1/3     │ 0.0s     │ 3    │       │
└─────────────────────────────┴───────────┴─────────┴──────────┴──────┴───────┘

silver.station_temperatures   passed   1 of 1 checks passed
  plausible_range             passed
```

All three assets succeeded. `bronze.readings` has eighteen rows, one for each reading, and the
other two have three, one for each station. The last two lines are the check: after
`silver.station_temperatures` was built, `plausible_range` ran against it and passed.

## Query

Ask the gold table for the stations, from the warmest to the coldest. The command is the last
one that `lakana init` suggested:

<!-- source: docs/examples/getting-started/run.sh#query -->
```bash
lakana sql "SELECT * FROM gold.station_ranking ORDER BY warmth_rank"
```

You will see this result:

<!-- output: getting-started#query -->
```text
┏━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ station_id ┃ avg_temp_c ┃ warmth_rank ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ C3         │ 9.0        │ 1           │
│ A1         │ 4.0        │ 2           │
│ B2         │ 1.0        │ 3           │
└────────────┴────────────┴─────────────┘
3 rows
```

Station C3 averaged 9 degrees, A1 averaged 4 and B2 averaged 1, so C3 is first.
CI runs every command on this page on every change. It checks the query result above and the
lines of the other outputs that stay the same: the tables of the deploy, and the status and row
counts of the run. Identifiers, commits, versions and durations differ on every machine and are
not checked.

A column may carry the name of its table or an alias, as in any SQL: `station_ranking.station_id`,
`gold.station_ranking.station_id` or `r.station_id` after `FROM gold.station_ranking AS r`.

## Stop the instance

Go to the first terminal and press Ctrl-C, or stop the instance from any terminal:

<!-- source: docs/examples/getting-started/run.sh#stop -->
```bash
lakana server stop
```

The instance shuts down and the command says so; if none runs, it says that instead and exits
without an error. Your tables stay where they are, and `lakana server start --local` brings the
instance back with them.

## Start over

To go through this tutorial again, or to throw away what you built, stop the instance as above
and reset it. The command names what it will delete and asks before it does:

<!-- source: docs/examples/getting-started/run.sh#reset -->
```bash
lakana server reset --local
```

It deletes the instance's database and the tables' files, and with them every table, secret and
token of this instance (the number of tables is in its question). It does not touch your
project: the folder `analytics` and its git history stay. It refuses while the instance runs,
and in any folder that is not a local instance's data directory. Afterwards,
`lakana server start --local` is a first run again, and this page works from the
"Create a project" step: delete the folder `analytics` first, or give the new project another
name. In a script, pass `--yes` to skip the question.

## Where next

- [Build a table with Python](python-sample.md) adds a Python asset to this sample, with
  `lakana init --template python`.
- [Check your data](../how-to/check-your-data.md) writes more checks, shows a failing one and what
  it does to a table.
- [Projects and deploys](../how-to/projects.md) covers Python assets, dry-run deploys, rollback, and
  selecting what to run. The keys you can write are in the reference:
  [asset front-matter](../reference/asset-front-matter.md) and [`lakana.yaml`](../reference/lakana-yaml.md).
- [Load data from a source](../how-to/load-data-from-a-source.md) reads files, a database or a
  REST API on a schedule, from a project that `lakana init --template` writes.
- [Give a second person access](../how-to/give-a-second-person-access.md) issues a token to a
  colleague who may query one environment.
- [Deploying with Compose (team mode)](../how-to/deploy-with-compose.md) moves from your machine to an
  instance a small team shares.
- [Scope and philosophy](../explanation/scope.md) says what Lakana is for, and what it is not for.
