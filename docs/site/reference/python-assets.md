# Python assets

The rules of a Python asset, what a deploy checks, what a task may do, how a failure is handled and
the operator's settings. A first example that builds a table from a Python asset is in
[Projects and deploys](../how-to/projects.md#a-first-example); why Python assets exist and where their
limits lie is in [Python assets: what they are for and how far they are trusted](../explanation/python-assets.md).

## The rules of a Python asset

- **One file, one asset.** `assets/<namespace…>/<name>.py` is the table `<namespace…>.<name>`.
  The function's name does not matter. Every `.py` file under `assets/` is an asset. Put helper
  code at the repository root or under `src/`; both are importable at run time. A helper module
  that has the same name as an installed package or a standard library module cannot be
  imported under that name, because the repository is searched last.
- **Inputs.** `inputs` maps each parameter of the function to a table or an artifact, except a
  parameter named `ctx`: `ctx` is not an input, it is reserved for the asset's context (see
  [The asset's context](#the-assets-context)), so `def customers(ctx):` needs no
  `inputs`, and `inputs` may not name it. A parameter
  that names a table receives the whole table as a `polars.DataFrame` (a table in memory). A
  parameter that names an artifact receives a `pathlib.Path` to a read-only copy of its files (see
  the [reference](artifacts.md)). It is read as the run principal,
  so row filters and column masks apply, and it is bound by the query limits
  (`LAKANA_QUERY_LIMITS__MAX_RESULT_ROWS`, default one million rows). If an input is too large,
  narrow it first with a SQL asset (usually a `view`) and declare that as the input. An asset
  that reads its own table declares it as an input. The table must exist before such an asset
  runs: the first run of an asset that reads itself fails at once, without retries, with
  `<table> reads its own table as an input, but the table does not exist yet`. Create the table
  with a first version of the asset that does not read itself, run it, then deploy the
  self-reading version.
- **Decimals.** DuckDB types a literal such as `12.0` as `DECIMAL`, a type of exact decimal
  numbers. Your function receives such a column as floating-point numbers (a `Float64` column),
  also inside a list or a struct, so `value * 0.5` works. An asset that must not round, for
  example one that adds up money, asks for the exact values with `@asset(decimals="exact")`: the
  column then arrives as a `Decimal` column of `decimal.Decimal` values, which cannot be
  multiplied by a float. A float holds about 15 significant digits: a decimal value beyond 2^53
  (about 9 * 10^15) or with more than about 15 significant digits, a decimal identifier or the
  sum of a wide column for example, arrives rounded, without an error. Such a column needs
  `decimals="exact"`. What your function returns is stored as it is: a float column as a
  double, a decimal column as a decimal. Older Lakana releases always
  handed a Python asset `decimal.Decimal` values; an asset written for them that relies on
  that needs `decimals="exact"` after an upgrade.
- **Connections.** `connections` names the connections of the environment this asset reads from,
  for example `@asset(connections=["crm-db"])`; inside the task, `lakana.ingest.connections.get`
  reads one with its secret
  ([How a task gets its credential](connections.md#how-a-task-gets-its-credential)). At most 16, each made of lower-case letters, digits and `-`, starting with a
  letter, and none listed twice. Each must exist in the environment you deploy to, or the deploy fails naming the
  `lakana connection create` command that creates it. A rollback to a version whose connection
  was deleted since fails the same way. While a deployed version declares a connection, the
  connection cannot be deleted. An asset without `connections` deploys as before. A project
  version in which an asset declares connections has manifest version 7 (8 when an asset also
  appends or merges), so a worker of an earlier release refuses its runs instead of running the
  asset without them: upgrade the workers first. The kinds of connection and the commands that manage them are in
  [Connections](connections.md).
- **Write modes.** `write`, `key` and `watermark` say what a run does to the table, with the
  rules of SQL front-matter (see [Write modes](asset-front-matter.md#write-modes)):
  `write="replace"` (the default) replaces every row with what the function returns;
  `write="append", watermark="updated_at"` creates the table on the first run and adds the rows
  of each later run; `write="merge", key=["id"]` updates the rows whose key the table has and
  adds the others, deleting none (`watermark` is optional). A key of several columns takes at
  most 5,000 rows per run. The platform does not filter what a Python asset returns: an append
  must return only the rows past `ctx.watermark()` (see [The asset's context](#the-assets-context)),
  or it adds every row again. A merge holds its whole result in memory while it compares it with
  the table, so what it returns counts against the worker's memory limit
  (`LAKANA_WORKER__MEMORY_LIMIT`; `LAKANA_RUN__TASK_MEMORY` for a task in a container).
  `materialize="incremental"` is refused with
  ``a Python asset appends with write="append" and watermark="<column>", not materialize='incremental'``.
  A project version in which an asset appends or merges has manifest version 8: a worker of an
  earlier release refuses it instead of replacing the table.
- **Output.** The function returns a `polars.DataFrame` or a `pyarrow.Table`; both get the same
  checks of their columns. It is written into the asset's table in one snapshot, as `write` says.
  Zero rows are fine: a replace empties the table, and an append or a merge whose source had
  nothing new writes nothing and succeeds. With `output="artifact"` it returns a `pathlib.Path` inside
  `lakana.output_dir()` instead, and each run stores a new version of the artifact (see the
  [reference](artifacts.md)).
- **Metadata.** `description` (default: the first paragraph of the docstring), `owner`, `tags`,
  `columns` (not for an artifact asset), `retries`, `retry_delay` and `timeout` work as in SQL front-matter, with the same
  defaults from `lakana.yaml` and the same operator maximums (see [Retries and timeouts](lakana-yaml.md#retries-and-timeouts)).
- **Only literals.** Deploy reads the decorator without running your code, so every argument
  must be written out as a literal: strings, numbers, lists and dicts of strings. Variables,
  `**unpacking` and function calls are refused.
- **Lineage.** A Python asset has dependency lineage (its inputs) and no column lineage. A SQL
  asset can read a Python asset's table by name; the run orders them as usual. A table written
  by an asset that declares connections shows each connection as a source in its lineage
  (`lakana table lineage`), recorded by the server from the deployed asset, never from what the
  task sends. Who sees it: [Connections](connections.md#lineage).
- **Who sees a dropped table in `table lineage`.** Lineage lists only tables you may read. A table
  that was dropped stays in the history of the tables it fed, marked `dropped`, and is shown only
  to someone who may read its whole namespace (a grant on the namespace, the environment or the
  instance) and never to a task token. For a table that was moved to another namespace before it
  was dropped, that is the namespace of the last name Lakana recorded for it, and the table is
  shown under that name. A table created later under the same name is a new table:
  a grant on it does not show the old one. It is counted in `hidden_nodes` instead. To let a
  colleague see the history, grant them read on the namespace.

## The asset's context

A function that has a parameter named `ctx` receives the context of its run there. Its one method
today is `ctx.watermark(default=None)`: the largest value of the asset's `watermark` column that
its table holds, as an `int`, a `decimal.Decimal`, a `datetime.date` or a `datetime.datetime`
(naive for a `timestamp` column, in UTC for a `timestamptz` one). On a first run the table has
no rows yet and `ctx.watermark()` returns `default`, so give the earliest value you want to load,
of the column's kind: a naive `datetime` for a `timestamp` column, one with `tzinfo=UTC` for a
`timestamptz` column (polars `Datetime` with a time zone), as here:

```python
from datetime import UTC, datetime

import polars as pl
from lakana import asset


@asset(write="append", watermark="updated_at")
def orders(ctx) -> pl.DataFrame:
    since = ctx.watermark(default=datetime(2020, 1, 1, tzinfo=UTC))
    rows = pl.read_parquet("exports/orders.parquet")
    return rows.filter(pl.col("updated_at") > since)
```

- **Filter the rows yourself.** The platform does not filter the rows a Python asset returns:
  filter them with `ctx.watermark()`. An append adds every row the function returns, so an asset
  that returns all rows on every run adds them all again.
- **Nothing new is fine.** A run that finds nothing new returns an empty table and succeeds
  without writing; the table, and so the next run's watermark, stay as they are. An empty
  `polars.DataFrame()` without columns is fine too, except on the first run, before the table
  exists (one a run created without rows exists): a table needs its columns, so that run fails
  with
  ``the asset returned no columns, so there is nothing to create bronze.orders from; its source had no data yet (check the pattern or the query); the first run must find data``.
- **At least once.** `append` is at-least-once: a run whose answer is lost (the worker stopped
  after the upload, before it could record it) runs again, and rows it returns again that are not
  past the next watermark are added twice. `merge` is idempotent: running it again changes nothing.
- **A concurrent write.** The worker reads the watermark before the task starts, and the upload
  commits only on the table it was read from. If another write lands in between, the attempt is a
  conflict and is tried again once at once, with the new watermark.
- **The watermark column.** It must be a column of what the function returns, of one of the
  types int, long, decimal, date, timestamp or timestamptz; otherwise the run fails before it
  writes, for example
  ``watermark column `name` is a string; it must be one of int, long, decimal, date, timestamp, timestamptz``.
- **Without a watermark.** `ctx.watermark()` returns `default` for an asset without `watermark`,
  such as a replace or a merge without one.

## Polars 1 and Polars 2

`lakana[assets]` asks for `polars>=1.30` and sets no upper bound, so a project that runs `uv lock`
now gets **Polars 2** (released in October 2026). Lakana runs assets on both, and its own tests run on
2 and on 1.30. A project that was locked earlier keeps the Polars version in its `uv.lock` until it
runs `uv lock --upgrade-package polars`. What changes for the code of an asset when a project
moves from 1 to 2 (the full list is Polars' own
[upgrade guide](https://docs.pola.rs/releases/upgrade/2/)):

- **`LazyFrame.collect()` uses the streaming engine** and does not keep the row order after joins
  and group-bys unless you ask. If your asset depends on an order, sort explicitly
  (`.sort(...)`) before you return the frame, or use `maintain_order` where the method offers it.
- **Map columns arrive as `pl.Map(key, value)`.** In Polars 1 they were a list of structs. A table
  stores no map, so only an input that a view returns can hold one, and returning a map column is
  refused (`Column 'x' has an unsupported Arrow type: map<…>`): turn it into columns first.
- **Adding a signed and an unsigned integer column gives `Int128`**, not a float. A table cannot
  store `Int128` (or `UInt128`): the task fails with `column 'x' has type Int128, which cannot be
  stored in a table; cast it to Int64 …`. Cast the result to `pl.Int64`, or to a decimal.
- **An Arrow extension type Polars does not know loads as `pl.Extension`.** Take its stored values
  with `pl.col("x").ext.storage()` before you return it.
- **Some casts are stricter** and raise where Polars 1 converted silently. A task that fails with
  a Polars `ComputeError` about a cast after an upgrade needs the cast to say what should happen.

To stay on Polars 1, pin it in the project's own `pyproject.toml` and lock again:

```toml
dependencies = ["lakana[assets]==<version>", "polars>=1.30,<2"]
```

Run `uv lock`, commit `uv.lock` and deploy. Polars 1.30 is the oldest version Lakana supports. One convenience needs a newer one: passing a
Polars `Schema` to `client.tables.create(...)` needs Polars 1.37 or newer. On an older Polars
the call fails with a message that says so; pass `pyarrow.schema(...)` or a list of columns
instead.
Spilling to disk, the one thing Polars 2 does that can use the worker's disk, is described under
"Polars inside a task" in [Run](../how-to/projects.md#run).

## What Python assets do not do (yet)

- A Python asset is a table (or an artifact), never a view. It appends with
  `write="append", watermark=...`, not with `materialize="incremental"`.
- Table inputs and a table output are whole DataFrames in memory. There is no streaming (#22), so
  the worker needs memory for your inputs, your output and your own work. An artifact is a file on
  disk and is not held in memory by Lakana.
- A changed output schema is a permanent failure: the asset cannot pass `evolve_schema`. Restore
  the columns and types the table has, or drop the table (`lakana table delete`) and run the
  asset again, so it is created with the new schema.
- No `async def`.
- `@asset(...)` must be the only decorator on the function. To wrap behaviour, do it inside the
  function.
- Local path and workspace dependencies in `uv.lock` are refused. Put helper code in the
  repository, or publish it as a package.
- No column lineage, no lazy input API, no resource classes.

## What a deploy checks

A deploy never runs your code. It reads each `.py` file with Python's `ast` module and the two
dependency files as TOML. Nothing is installed at deploy. Every refusal names the file and, where
there is one, the line, as `path:line: message`. Fix the cause and deploy again.

| The deploy says | Cause | Next step |
|---|---|---|
| `no function decorated with @asset; the decorator must be lakana.asset: …` | The file has no `@asset(...)` function, or the decorator is written another way (an alias, a re-export). | Write `from lakana import asset` and `@asset(...)`, or `import lakana` and `@lakana.asset(...)`. |
| `more than one @asset function; one file is one asset` | Two decorated functions. | Move one into its own file. |
| `write @asset(...) with parentheses` | `@asset` without `(...)`. | Write `@asset(inputs={...})`; use `@asset()` for none. |
| `the asset function must have @asset(...) as its only decorator` | Another decorator sits on the function. | Remove it, or call the wrapped code inside the function. |
| `argument 'x' must be a literal`, `unknown argument 'x'`, `argument 'x' must be a dict of strings` | The decorator has a variable, an unknown name or the wrong type. | Write the value out, and use only the arguments listed above. |
| `parameter 'x' has no entry in inputs`, `inputs names 'x', which is not a parameter` | The function's parameters and `inputs` differ. | Make them the same set of names. |
| `positional-only parameters are not supported`, `*args is not supported; …`, `**kwargs is not supported; …`, `a parameter must not have a default` | The signature has a shape the harness cannot fill. | Declare every input as a plain parameter. |
| `an asset function must not be async` | `async def`. | Use `def`. |
| `input 'x': …` | The input is not a valid table name. | Use `namespace.table`. |
| `Secret 'x' of environment 'e' is not bound to a use yet` (or `is bound to …, not to repository 'r'`) | The repository names a credential secret that nobody bound to it, or that serves something else. Lakana sends a secret only to the use it is bound to. | Someone who manages secrets runs the command the message ends with: `lakana secret bind x --env e --repository r`. To stop sharing one secret between repositories, create one per repository: `lakana secret create NEW --env e --repository r --values-stdin`, then `lakana repo update r --credential-secret NEW`. |
| `secret 'x' does not exist in environment 'e'. Create it with …` | The repository names a credential secret that the environment lacks. | Run the command the message shows; it ends in a bound secret. |
| `lakana.yaml: python: must be one of "3.11", "3.12", "3.13", "3.14" …` | The version names no task image, or is written without quotes (YAML reads `3.10` as a number). | Write one of the four, in quotes. See [The Python of your tasks](lakana-yaml.md#the-python-of-your-tasks). |
| `table 'x' not found` | An input is not produced by this repository and does not exist in the environment, or the deploying principal may not read it. When two repositories share an artifact, this is also what the consumer's deploy says if it comes **before** the producer's first deploy. | Deploy the repository that produces it first, or fix the name. See "Two repositories, one artifact" below. |
| `` `models.x` is an artifact; only a Python asset can read it. `` | A SQL asset names an artifact in `FROM`. | Read it in a Python asset. |
| `` `x.y` already exists in dev as a table; drop it first, or choose another name. `` (or `a view`, `an external table`) | An artifact asset has the name of a table, view or external table. | Choose another file name, or drop the table with the `table delete` subcommand. |
| `` `x.y` already exists in dev as an artifact; drop it first (`lakana artifact drop x.y`), or choose another name. `` | A table asset has the name of an artifact. | Choose another name, or drop the artifact. |
| `parameter 'x' reads the artifact …, which the task stores in a directory of that name: rename the parameter …` | The parameter that reads an artifact is not letters, digits and underscores, or is longer than 128 characters. | Rename it in the function and in `inputs`. Two such parameters may not differ only in case. |
| `an artifact asset cannot read its own artifact (x.y); remove it from inputs` | An artifact asset lists itself as an input. | Remove it. |
| `argument 'output' must be 'table' or 'artifact', got …`, `argument 'keep_versions' must be an integer of at least 1`, `argument 'materialize' does not apply to an artifact asset; remove it` | A wrong value for an artifact option. | See the [reference](artifacts.md). |
| `` `output` applies only to a Python asset, not to a SQL file; declare the asset in a `.py` file with … `` | `output`, `keep_versions`, `decimals` or `connections` in the front matter of a SQL file. | Declare the asset in a `.py` file, or remove the key. |
| ``Asset `x.y` declares connection `c`, which environment `e` does not have. Create it: `lakana connection create c --env e --kind <kind> …` `` | The environment has no connection of that name: it was never created there, or was deleted. A rollback says the same. | Create it with the command the message shows (`lakana connection create --help` lists the kinds), or remove the name from `connections`, then deploy again. |
| ``Asset `x.y`: the connections this project version declares cannot be read, so they cannot be checked against environment `e`. Deploy the repository again to record a new version.`` | The stored record of a project version was damaged or edited by hand. A rollback to such a version says the same. | Deploy the repository again; roll back only to a version recorded intact. |
| `argument 'connections' must be a list of strings`, ``connection `c` is listed twice``, `an asset declares at most 16 connections`, `connection names are lower-case letters, digits and '-', …` | A wrong value for `connections`. | Write a list of distinct connection names, such as `connections=["crm-db"]`. |
| `the file does not parse: …` | A syntax error. | Fix the line it names. |
| ``a Python asset appends with write="append" and watermark="<column>", not materialize='incremental'`` | `materialize="incremental"`. | Write `write="append", watermark="<column>"`. |
| `a Python asset supports only materialize 'table', got …` | `materialize` is neither `table` nor `incremental`. | Remove the argument, or write a SQL asset. |
| ``write must be one of replace, append, merge; got '…'``, ``` `write: append` needs `watermark:` … ```, ``` `write: merge` needs `key:` … ```, ``` `write: replace` takes neither `key:` nor `watermark:` ```, ``key column `id` is listed twice`` | A wrong write model. | See [Write modes](asset-front-matter.md#write-modes); the rules are the same as in SQL front-matter. |
| `` `ctx` is the asset's context; give the input another parameter name `` | `inputs` names a parameter `ctx`. | Rename the parameter and its entry in `inputs`. |
| `assets/a.sql and assets/a.py both produce table 'a'` | A SQL file and a Python file for one table. | Keep one. |
| `every .py file under assets/ is an asset; put helper code outside assets/ …` | `__init__.py` or a helper module under `assets/`. | Move helper code to the repository root or `src/`. |
| `symlinks are not allowed in a repository with Python assets` | A symlink anywhere in the repository. | Replace it with a copy of the file. |
| `a project with Python assets needs a pyproject.toml at the repository root` | No `pyproject.toml`. | Add one (see the [first example](../how-to/projects.md#a-first-example)). |
| `a project with Python assets needs a committed uv.lock; run `uv lock`` | No lock file. | Run `uv lock`, then commit it. |
| `does not contain lakana; add lakana[assets] …`, `does not contain polars; depend on lakana[assets] …` | The lock lacks the SDK or polars. | Depend on `lakana[assets]` and run `uv lock`. |
| `lakana is locked, but no task installs it: it is only in a dependency group or an extra …` | `lakana` is in `uv.lock` only through a dependency group (for example `uv add --dev`) or an extra, and a task installs neither. | Add `lakana[assets]` to `[project] dependencies`, run `uv lock`, commit and deploy again. |
| `has lakana X, but this instance runs Y (…); update the dependency and run `uv lock`` | The locked `lakana` does not match the instance. Before 1.0 the minor versions must match; from 1.0 the major versions. | Change the pin, run `uv lock`, commit. |
| `local path and workspace dependencies are not supported (…)` | The lock installs a package from a path in the repository. | Put the code in the repository (it is importable) or depend on a released package. |
| `uv.toml: is not supported in a project with Python assets …` | A `uv.toml` at the repository root. The worker builds with `uv sync --frozen --no-config`, which ignores it. | Put settings that shape the lock into `[tool.uv]` in `pyproject.toml` (they apply when you run `uv lock`), delete `uv.toml`, run `uv lock`, commit and deploy again. A task of an older deploy that has one fails with the same message until you redeploy. |
| `uv.lock: takes X from the package index URL, which is not registered on this instance …` | A package comes from an index nobody registered on this instance. | Ask an administrator to register the index (`lakana index add NAME --url URL`), or take the package from a registered index (`lakana index list`), then run `uv lock`. See [Use a private package index](../how-to/private-package-index.md). |
| `uv.lock: takes X from the package index URL, which is not a registered package index, and this URL cannot be registered as written: …` | A package comes from an index URL that no administrator could register (`http` for a remote host, a user name, `%` or `..` in the path, a query). | Take the package from a registered index (`lakana index list`), or publish it to an index with a plain `https` URL; the message says which rule the URL breaks. |
| `uv.lock: takes X from a file outside the folder `wheelhouse` …` | A wheel or source distribution of `wheelhouse` has a path with `..`, a leading `/` or a backslash. | Put the file into `wheelhouse`, then run `uv lock`. |
| `uv.lock: takes X from the local folder F; the only folder a lock may name is `wheelhouse` …` | A package comes from a folder of the project other than `wheelhouse`. | Rename the folder or take the package from a package index, then run `uv lock`. |
| `uv.lock: takes X from a git repository, which is not supported …`, `… from a direct URL, which is not supported …` | A package comes from a git repository or a single file URL. | Publish the package to a package index, ask an administrator to register that index, then run `uv lock`. |
| `pyproject.toml: the index N (URL) is a registered package index and needs `explicit = true` …` | A registered index is declared without `explicit = true`, so `uv lock` could take any package from it, or take one of its packages from the public index. | Add `explicit = true` to its `[[tool.uv.index]]` table and one line per package under `[tool.uv.sources]` (`PACKAGE = { index = "N" }`), then run `uv lock`. |
| `pyproject.toml: the index named N points at URL, but the package index registered as M is URL2 …` | The project's index has the name of a registered index and another URL. Checked at a deploy only, and not for an index whose `url` is a folder, such as `wheelhouse`. | Use the registered URL, or give your index another name, then run `uv lock`. |
| `pyproject.toml: `extra-index-url` names the registered package index N …` (or `index-url`) | A registered index is named as `index-url` or `extra-index-url`. | Declare it as a `[[tool.uv.index]]` table with `explicit = true` instead, and name its packages under `[tool.uv.sources]`, then run `uv lock`. |
| `requires-python '…' does not admit the worker's Python X` | The project's Python range excludes the worker's Python. | Widen `requires-python`, then `uv lock`. |
| `requires-python '…' does not admit Python X, which python: in lakana.yaml names` | The project's Python range excludes the Python that `python:` in `lakana.yaml` chooses. | Widen `requires-python` and run `uv lock`, or name another Python. |
| `this project names Python X in lakana.yaml, and this worker runs Python tasks on its own Python Y without containers …` | The worker runs tasks without containers (`LAKANA_RUN__RUNNER=local`), so it has only its own Python. | Run the project on a worker with `LAKANA_RUN__RUNNER=docker`, or set `python:` to the worker's version (or remove it) and deploy again. |
| `environment 'E' is protected, and this worker runs Python tasks without isolation (LAKANA_RUN__RUNNER=local) …` | A protected environment runs Python assets only in containers, and this worker runs tasks as its own processes. The task never started. | Run the task on a worker with `LAKANA_RUN__RUNNER=docker` (the Compose bundle's default), then run again. See [Run Python tasks in containers](../how-to/run-python-tasks-in-containers.md#protected-environments). |
| `environment build failed: the task's log could not be written while its environment was built …` | The worker could not keep the build's output. | Read the worker's log for the cause, then start the run again. |
| `this instance cannot run Python assets: …` | The worker has no `LAKANA_RUN__SERVER_URL`, or no `uv`. | Ask whoever runs the instance; see "Operator settings" below. |

## What a task may read and write

Each attempt of a Python asset is a **task** that runs in its own process on a worker, or, with
the docker runner, in a container of its own ([Run Python tasks in containers](../how-to/run-python-tasks-in-containers.md)). The
worker gives the process a **task token**: a credential that is valid for this one attempt and
only while it runs. With it, your code can:

- read the tables and artifacts declared in `inputs` (and, when an input is a view, the tables that
  view reads: a view is not a security boundary here);
- replace its own table, or add a version to its own artifact if it produces one, whether or not
  the environment is protected (a token belongs to one environment);
- read the deploys and runs of its environment.

It can do nothing else: no other table or artifact, no `DROP`, no deleting an artifact version,
no listing of tables, no administrative action. It also cannot do more than the run principal could (see [Run principals](../how-to/projects.md#run-principals)): grant that
principal `READ_TABLE` on the inputs (tables and artifacts) and `CREATE_TABLE` and `WRITE_TABLE` on
the output. The
token never appears on a command line, in the environment or in the task log.

The worker unpacks your repository fresh for every attempt into a private, read-only
directory, which is the process's working directory. The process gets a private temporary
directory and a small environment (`PATH`, `HOME`, `TMPDIR`, two `PYTHON*` settings, a Polars
spill directory and, when the worker has a memory limit, two more `POLARS_*` settings, see
"Polars inside a task" in [Run](../how-to/projects.md#run)). Its
output and the output of the environment build are in `lakana runs logs`, up to 1 MiB per
attempt (see [Run](../how-to/projects.md#run)).

## Failure, retry and cancel

| Situation | What happens |
|---|---|
| Your function raises | The task fails with the exception's type and message; the traceback is in the task log. Retried under the asset's policy. |
| A SQL query with a syntax error | Fails with `SQL syntax error at line 1, column 7: …` (the reason, the position and the token it stopped at; the cause is `invalid_query`). Not retried: the same SQL fails again. Fix the query. |
| A SQL query nested too deeply (hundreds of parentheses) | Fails with `SQL syntax error: the query is nested too deeply. Simplify it …`; the cause is `invalid_query`. Not retried. |
| A view whose stored SQL no longer parses | Fails with `View 'gold.v' could not be read. Ask its owner to recreate it.` and shows none of the view's SQL. |
| A file that cannot be loaded (a syntax error, a module that is not installed, no `@asset` function) | Fails with a message that names the cause. Not retried: the same code fails again. |
| An input cannot be read | Fails with `reading input 'obs' (silver.weather) failed: …`. The server says why the input query failed, and the task is **not retried** when the cause repeats: the input table does not exist (dropped or renamed), the run principal may not read it, or the input is too large. Too large also says to narrow it with an upstream SQL asset (for example a view) and declare that as the input. Running out of memory (cause `out_of_memory`) is retried, and its message says to narrow the input with an upstream SQL asset or to ask the operator to raise the memory limit that the message names (`LAKANA_QUERY_LIMITS__MEMORY_LIMIT` for a query that failed on its own limit, `LAKANA_WORKER__MEMORY_LIMIT` when the whole worker was stopped). A sort or aggregation over DuckDB's limit is out of memory too, because DuckDB cannot spill to disk there. Any other failure (a timeout, a storage error, a query the engine rejects, an unknown cause) is retried until `retries` runs out. A 401, 403 or 404 from the API itself is not retried either. A server older than this behaviour sends no cause, and its failed input queries are retried as before. |
| The worker ended a task's query alone for using too much memory (Linux; cause `out_of_memory` on the query run) | The message says the query made the worker exceed its memory limit and names the setting to raise. A SQL task is **not retried**. A Python task whose input query was ended is handled like any input that ran out of memory: retried while it has attempts left, and its message says to narrow the input with an upstream SQL asset or to ask the operator to raise the limit. The worker and a Python task beside the query keep running. |
| The worker ended this task alone because it was the largest consumer over the worker's memory limit (Linux) | The attempt fails as a transient failure with `this task was ended because the worker exceeded its memory limit (4GB) and the task was the largest consumer. Use less data in the task (narrow its inputs with an upstream SQL asset), or ask the operator to raise the limit (LAKANA_WORKER__MEMORY_LIMIT, or LAKANA_WORKER_MEMORY in the Compose bundle's .env).` The task is **retried** while it has attempts left, even when it was the only task: the worker's own memory may have been part of the total. A task that always needs more than the limit fails every attempt. The worker and a query beside it keep running. A task still building its environment cannot be ended this way; if nothing else can be ended, the worker stops. |
| The worker stops for using too much memory (cause `out_of_memory` on the query run) | The message names the limit and the setting to raise. On Linux a running Python task is always ended first and retried (the row above), so this row reaches only a SQL task and a Python task still building its environment. Such a task is **not retried** when the worker was running only this task (and its own query runs): it would need the same memory again. A SQL task whose write had already been committed is `succeeded` instead (the row above; a task whose table has [checks](../how-to/check-your-data.md) is taken again and only checks). On macOS every task is handled this way. When an unrelated query was running on the worker too, the blame is uncertain and the task is retried while it has attempts left. |
| A Python asset raises `MemoryError` | Fails with `MemoryError. The task ran out of memory: use less data …, or run it on a machine or in a container with more memory.` Not retried. The machine or container itself had no memory left: narrow the inputs with an upstream SQL asset, work in smaller pieces, or give the worker's machine or container more memory. |
| A Python asset's function raises `TypeError` | Fails with `TypeError: <the exception's text>` and a second line: `This error is almost always a mistake in the asset's code, so it is not retried. Fix the function, deploy, and run again.` Not retried: a wrong type is almost always a mistake in the code, and every attempt repeats it. If the cause can be transient (a library raising `TypeError` on a value an HTTP call did not return this time, for example), catch it in the function and raise another exception type, such as `RuntimeError`, which is retried. This holds for a `TypeError` (or a subclass) raised out of the function itself, not for one raised while the file is imported. Any other exception of the function (`ValueError`, `KeyError`, `NameError`, a failed network call, ...) is retried until `retries` runs out, because it can depend on the data or on the moment. |
| A return value that cannot become a table | Fails and is not retried: `the asset returned a polars LazyFrame; call .collect() …`, `… a pandas DataFrame; return pl.from_pandas(df)`, ``… a pyarrow RecordBatchReader; read it with `.read_all()` and return the table``, `… a DataFrame without columns` (`… a table without columns` for a `pyarrow.Table`), or `column 'x' has type …, which cannot be stored in a table`. A column of type `Int128` or `UInt128` is refused by name, with the way out: `cast it to Int64 (pl.col('x').cast(pl.Int64)), or to a decimal such as pl.Decimal(38, 0) if the values do not fit into 64 bits` (Polars 2 gives `Int128` when you add a signed and an unsigned integer column). An `Extension` column (an Arrow extension type Polars does not know) is refused with `take its stored values with pl.col('x').ext.storage() and cast them to a type a table stores`. A table stores no list, struct or map either: such a column is refused as `Column 'x' has an unsupported Arrow type: …`. |
| The output schema changed | Fails and is not retried: `the asset's output no longer matches the schema of <table> …`, with the remedy. |
| An append or merge's first run returns no columns | ``the asset returned no columns, so there is nothing to create <table> from; …``. Not retried. Run it again once its source has data. |
| The watermark column is missing or has another type | ``watermark column `ts` is not a column of what the asset returned; return it, or change the asset's watermark=`` or ``watermark column `name` is a string; it must be one of int, long, decimal, date, timestamp, timestamptz``. Not retried. |
| A merge's rows repeat a key, or a key of several columns has more than 5,000 rows | ``key `id` repeats in 4 rows of what the asset returned; return one row per key, for example with `df.unique(subset=['id'], keep='last')` `` or ``a merge on a key of several columns takes at most 5,000 rows per run, and the asset returned 12,000. …``, which says to merge on one key column or to return only the rows past `ctx.watermark()`. Not retried; nothing is written. |
| A connection the asset declares no longer exists | ``<table> declares the connection `crm-db`, which environment dev does not have; create it (lakana connection create) or remove it from the asset's connections, then deploy again``. Not retried. |
| The process exits or is killed without a result | `the task process exited with status N without a result` or `the task process was killed by signal N`. Retried. A kill by signal 9 is usually the operating system running out of memory, and signal 6 is an abort, as after a failed allocation in native code: the fix is less data in memory (narrow the inputs) or a worker with more memory, not another retry. |
| An artifact input has no version, or cannot be read | `models.x has no version yet; run the asset that produces it` (not retried), or `models.x cannot be read by the run's principal 'svc-run', or does not exist; grant read on it or check the name` (not retried). Download problems say `reading input 'model' (models.x, version 3) failed: …`; a version that was deleted since the task started is retried and resolved again, and a refusal such as a grant revoked after the task was prepared is not retried. A version whose stored manifest the client rejects (it does not match its own digest, or it is over a limit such as a path the client refuses) is not retried either: every attempt would meet the same record. Its message says so (`The stored manifest of version 3 does not match its digest, so this version is damaged on the server and downloading it again will not help. Tell the operator, and store the artifact again …`). A downloaded file that does not match its manifest, a broken connection and a 5xx are retried. |
| An artifact upload is refused as too large (413) | The task fails with the server's text, for example `The manifest request is larger than 16777216 bytes. It lists every file with its path and hash: store fewer files or give them shorter paths (the limit is fixed).` Not retried: the operator's limit is reached, and the same upload is refused again. Store fewer or smaller files. |
| A file the asset wrote cannot be read while the artifact is stored | `<path> could not be read: <the operating system's reason>. The upload was stopped and can be tried again once the file is readable.` Not retried: the task fails with the reason (a disk error, a file system that went away). A file that changed while it was sent says `<path> changed after it was scanned …` instead. |
| The kind of an input or of the asset's own name changed since the deploy | `models.x is a table, but this deploy recorded it as an artifact; redeploy so that the asset reads it as a table` (or `a view`, `an external table`), `models.x is an artifact, but this deploy recorded it as a table; redeploy so that the asset receives a path`, or, for an artifact asset whose own name is now a table, ``models.x is a table, not an artifact. Drop it (`lakana table delete models.x`) or give the asset another name and redeploy.`` (or `a view`, `an external table`). None is retried. Redeploy the consuming repository, or drop the table with the command the message gives. |
| Code of an asset stores or writes the wrong kind of output | `Task 'models.x' produces a table; it cannot add an artifact version. To store an artifact, declare the asset with output="artifact".` or `Task 'models.x' produces an artifact; it cannot write a table. Write the artifact's files into lakana.output_dir() instead.` Declare the output you mean, and return the path or the DataFrame. |
| An artifact has 16 uploads in progress | ``models.x has 16 uploads in progress, the most allowed at once. Commit one, withdraw one you began (`lakana artifact uploads list models.x` shows yours, `lakana artifact uploads abort models.x UPLOAD_ID` withdraws one), or wait for one to expire (they expire after 3600 seconds), then try again.`` The attempt is retried. A new attempt first deletes the open uploads of its own task run's earlier attempts, so only uploads of people and of other task runs can fill the places; wait for the retry, or withdraw an upload you began ([List and withdraw an open upload](../how-to/withdraw-an-artifact-upload.md)). |
| The task's process is killed or times out after its version was stored | The task shows as failed or timed out and is retried; its version **stays live**, and a retry stores a newer one. With `keep_versions=1` the previous version is already deleted, so use at least 2 if you want the last good model to survive a bad run. |
| The worker is older than the platform that deployed the project | `the project version's manifest has version 5, but this worker understands versions 1 to 4; update the worker to the platform version that deployed it, or redeploy with this worker's version` (the numbers differ). Not retried. Update the worker. |
| An artifact asset returns the wrong thing | `an artifact asset must return a pathlib.Path inside lakana.output_dir() …`, `the asset returned …, which is not inside lakana.output_dir() …`, `… which does not exist …`, `… which passes through a symbolic link …`. Not retried. See the [reference](artifacts.md). |
| The task reports success but its table did not change | `the task reported success without writing its table`; not retried. An attempt counts as succeeded only when the platform finds the table changed, or, for an append or a merge, unchanged after a run that reported no rows (nothing new). For an artifact asset the platform looks for a version stored by this attempt and says `the task reported success without storing a version of its artifact …`. |
| The environment build fails | `environment build failed: …`, with uv's output in the log. Retried. A build that runs out of time or space says so. |
| A package index refuses the credential of a registered index | ``environment build failed: The package index 'team' refused the credential in secret 'team-index' of environment 'dev'. …`` Not retried: the same credential is refused again. Set the secret again with the command in the message (`lakana secret rotate team-index --env dev --values-stdin`); the next run builds the environment with it, without a new deploy. See [Use a private package index](../how-to/private-package-index.md). |
| The secret of a registered index is missing, has no usable `password`, or cannot be decrypted | ``environment build failed: the package index 'team' needs the secret 'team-index' in environment 'dev', which does not exist. …`` or ``environment build failed: the secret 'team-index' of environment 'dev', which the package index 'team' uses, has no usable "password". …``. Not retried. Create or set the secret with the command in the message. A deploy stops with the same text, and a run whose environment is already built does not read the secret. |
| A package has no wheel for this platform and Python, and a registered index with a credential is in use | `environment build failed: sdonly has no wheel for this platform and Python; publish a wheel to your index, or remove the dependency. …` Not retried. A project that uses a package index with a credential installs wheels only: no package's build script runs while the credential is in reach. |
| The asset reads its own table, which does not exist yet | `<table> reads its own table as an input, but the table does not exist yet …`. Not retried. Create the table with a version of the asset that does not read itself, then deploy the self-reading version. |
| The task's environment has no `lakana` | `the task's environment does not contain the lakana package …`. Not retried. A deploy normally refuses this earlier; it can still happen when `lakana` is only reachable through an extra that is not switched on, or is excluded on the worker's platform by a marker. Add `lakana[assets]` to `[project] dependencies`, run `uv lock`, commit and deploy again. |
| The venv cache is not private | `environment build failed: the venv cache directory is not private …`. Not retried; the operator fixes the directory. |
| Timeout | The process and its process group are killed: `timed out after 600s`. Retried. The timeout starts when your process starts; the environment build does not count, but the wait of your input queries does. |
| Another writer changed the table during the run | `<table> changed during the write; retry.` The platform retries once at once, without counting an attempt, so the function reads fresh inputs. A second conflict is an ordinary failed attempt. |
| Cancel (`lakana runs cancel`) | The process is killed and the task is cancelled, with the message `The run was cancelled.` (`The run was stopped because another task failed its code integrity check.` when the platform stopped the run for that reason). During an environment build, the build is stopped too: its process (or, with the docker runner, its container) is killed, nothing of the half-built environment is kept, and the task is cancelled. The worker learns of a cancel at its next lease renewal (`LAKANA_RUN__RENEW_SECONDS`, 20 s by default), for a build as for a running task. |
| The worker is stopped (a drain) or lost | The task is released to another worker; a stop is not counted as an attempt. A SQL task that is lost is tried again under its policy, except in the next two rows. |
| The worker is lost, or stops itself for its memory limit in a way that ends the task for good, **after a Python task with checks may have written its table**, on the task's last attempt, or in a run that you cancel (a worker that sees the cancel after the write hands the task back in the same way) (also a SQL task with checks whose write landed, and also when you cancel after the task already waits again for its checks or its next attempt) | The task does not end at once. It waits with the reason `setting_back`, and the next free worker only sets the table back to where it started, without running the task or its checks. Then the task is `failed` (or `cancelled`), and its message says what became of the table: see [A worker that is lost](../how-to/check-your-data.md#a-worker-that-is-lost). A Python task without checks records no baseline, so a lost one leaves what it wrote. |
| The worker is lost, stopped for its memory limit, or fails to record the outcome, **after a SQL task's write was committed** (a task whose table has [checks](../how-to/check-your-data.md) is the exception: it is taken again and only checks) | The task is `succeeded` and is **not** run again, also on its last attempt and also while the run is being cancelled (the data is in the table). When the worker was lost, its message is `The worker was lost after this task's write had been committed. The task counts as succeeded and was not run again.` (for a memory stop it says that, with the limit's setting); when the worker was alive and only recording the outcome failed, it may carry a message that says so, or none; its attempt is `succeeded` with the rows written, and the tasks below it go on. It has no watermark or schema change, because the worker never recorded them, and, when the worker was lost, its finish time is the moment the platform found the commit, so its duration includes the wait for the lease to run out. Only the write of the task's **current** attempt counts: a write that an earlier attempt committed and lost is not looked for. |
| The lock names a package index that is no longer registered | `… The package indexes registered on this instance no longer match this project version: register the index again …`. Not retried; it fails for good. Ask an administrator to register the index again (`lakana index list` shows what is registered) and start the run again: no deploy is needed. Or change the project as the message says and deploy again. |
| The project declares an index that is registered now in a way that is not allowed | `… A package index is registered on this instance that the project declares in a way that is not allowed: declare it as a `[[tool.uv.index]]` table with `explicit = true`, …`. Not retried. Registering the index again does not help: declare it with `explicit = true`, name its packages under `[tool.uv.sources]`, run `uv lock` and deploy again. |
| The lock no longer fits the instance (after a platform upgrade) | `… This project version was deployed for another platform version: update the lock and redeploy.` Not retried; retrying the run cannot fix it. Lock again and deploy, unless the lock's `lakana` version in the message is higher than the one the instance runs: then a worker of an older release picked the task up, and the fix is to upgrade that worker (see [Upgrade to the release with artifacts](../how-to/upgrade-to-artifacts.md)). |

A task's token must outlive the task. If `LAKANA_TASK_TOKENS__MAX_TTL_SECONDS` is below the
asset's timeout plus the kill grace (`LAKANA_RUN__KILL_GRACE`, 10 s) plus 60 s, the task
fails at once and the message names the value to set.
Retries, retry delay and timeout follow [Retries and timeouts](lakana-yaml.md#retries-and-timeouts).

## Dependencies

A project with a Python asset has a `pyproject.toml` and a committed `uv.lock` at the repository
root. The lock must contain `lakana` at a version compatible with the instance (the same minor
version before 1.0, the same major version from 1.0) and `polars`; depending on `lakana[assets]`
gives you both. It must be one of the project's own `[project] dependencies`: a task installs no
dependency group and no extra, so `lakana` in `[dependency-groups]` is refused at deploy. `requires-python` must admit the worker's Python. Your repository's own code is
not installed: it is imported from the checkout.

`lakana[assets]` holds `polars`; for `lakana.ingest.files`, `fastexcel` (spreadsheets) and
`obstore` (S3, GCS and Azure Blob Storage); and for `lakana.ingest.sql`, the ADBC drivers
`adbc-driver-manager`, `adbc-driver-postgresql` and `adbc-driver-sqlite`. A project locked
before they joined it runs `uv lock --upgrade-package lakana` to add them; see
[the ingest helpers](ingest.md).

The first task that needs an environment builds it from the lock (`uv sync --frozen`) and caches
it, keyed by the hash of the lock together with your `pyproject.toml`, the Python version and the
platform. With the local runner, projects whose lock and `pyproject.toml` are both identical
share the environment; with the docker runner an environment belongs to one project in one
environment. A change to either file builds a new one.

Put a Python asset's dependencies in `[project] dependencies`. Packages in a dependency group
(`[dependency-groups]`, including `dev`, and `[tool.uv] default-groups`) are not installed for a
task. After a platform upgrade that changes the compatible `lakana`
version, update the lock and redeploy: tasks of the old deploy fail with a message that says so. To install a package of your own from an index with a password, see [Use a private package index](../how-to/private-package-index.md).

What the build can run: it installs only what the lock names, but a package that is built from
source (a source distribution) runs its own build backend, and it can see the index and proxy
credentials the worker passes to the build. With the local runner it runs as the worker's user,
with the worker's network access; with the docker runner it runs in the build's own container,
which reaches only the package indexes' hosts. That is why a build that uses a registered index's credential builds nothing from source: uv runs with `--no-build`, and a package without a wheel fails the build with a message. The worker builds with `uv sync --frozen --no-config`: your `[tool.uv]`
settings shape the lock on your machine (when you run `uv lock`) and are ignored at build time on
the worker (a `uv.toml` is refused, see [What a deploy checks](#what-a-deploy-checks); the worker still reads the lock and
the index definitions in `[[tool.uv.index]]`), and the lock decides which hosts the build
contacts. Prefer wheels, and review dependencies as you review code.

The lock may name only the public index, the folder `wheelhouse`, the operator's extra index and
registered package indexes; see [What a deploy checks](#what-a-deploy-checks) and [Use a private package index](../how-to/private-package-index.md).

## Operator settings

| Variable | Default | Meaning |
|---|---|---|
| `LAKANA_RUN__SERVER_URL` | unset | The server's `http(s)://host[:port]` URL as a task's process reaches it, without credentials, query or fragment. Unset, the worker refuses Python assets (a deploy says so; the worker's log says so at startup). The Compose bundle and local mode set it. |
| `LAKANA_RUN__VENV_CACHE` | `<worker cache>/venvs` | Local runner only. Where venvs are kept. An absolute path. It must be private: owned by the worker's user, mode `0700`, not a symlink; otherwise tasks fail with "not private". |
| `LAKANA_RUN__VENV_KEEP` | `10` | Local runner only (the launcher keeps 10). Venvs kept, most recently used first (at least 1). A venv in use is never removed. |
| `LAKANA_RUN__VENV_BUILD_TIMEOUT` | `600` | Seconds one build may run (above 0). |
| `LAKANA_RUN__VENV_MAX_BYTES` | `5368709120` (5 GiB) | Largest size of one venv while it is built (above 0). |
| `LAKANA_RUN__KILL_GRACE` | `10` | Seconds between SIGTERM and SIGKILL when a task is stopped (0 or more). |
| `LAKANA_RUN__SWEEP_INTERVAL` | `3600` | Seconds between the worker's sweeps of attempt directories and deploy checkouts a dead worker left behind (the first runs at startup) and between the sweeps that the worker holding the scheduler role runs of the artifact store (expired uploads, files and manifest objects no version names), of table locations (the files of creates that never finished) and of the code archives of failed deploys. `0` turns all five sweeps off (0 or more); then nothing collects those leftovers. |
| `LAKANA_RUN__INDEX_URL` | unset | An extra package index for the build (passed to uv as `UV_INDEX`). A credential goes in the URL's userinfo (`https://user:token@host/simple`, redacted from logs); a URL with a query string (`?token=...`) is refused at startup. Only userinfo is redacted from logs, **a token in the path is not**. The server and the worker do not refuse such a URL: when a path segment looks like a token (16 or more letters, digits and `-_.~%+`, with a digit or a capital letter among them; the form `https://host/<token>/simple/`), they start and log one warning at startup that names this setting and not the URL. If it is a token, move it into the userinfo; if it is a project or repository name, nothing needs doing. A token shorter than 16 characters in the path is not recognised, so there is no warning and it is not redacted: do not put one there. |
| `LAKANA_RUN__HTTPS_PROXY` | unset | Local runner only: refused with `docker`. Proxy for the build. A credential goes in the userinfo (`http://user:password@host:3128`); a URL with a query string is refused at startup; a path segment that looks like a token is warned about at startup, as for `LAKANA_RUN__INDEX_URL`. |
| `LAKANA_RUN__NO_PROXY` | unset | Local runner only. Hosts that bypass the proxy. |
| `LAKANA_RUN__CA_BUNDLE` | unset | Both runners. An absolute path of a PEM file of private certificate authorities that builds and tasks trust in addition to Mozilla's roots; only its certificate blocks are used, and a file with a private key, without a certificate or over 1 MiB stops the worker at start. See [The trust bundle of builds and tasks](network-settings.md#the-trust-bundle-of-builds-and-tasks). |
| `LAKANA_RUN__RUNNER` | `local` | How Python tasks run: `local`, as processes of the worker (not isolated from it), or `docker`, each task and each environment build in a container of its own that the launcher service starts. Another value stops the worker at start. |
| `LAKANA_RUN__LAUNCHER_SOCKET` | `/run/lakana-launcher/launcher.sock` | With `docker`: the launcher's socket, in a volume the worker and the launcher share. An absolute path. |
| `LAKANA_RUN__TASK_MEMORY` | `2GB` | With `docker`: the memory limit of each task container and build container, outside the worker's own limit. Binary units (`1GB` is `1024MB`), at least `64MB`. |
| `LAKANA_RUN__TASK_CPUS` | `1` | With `docker`: the CPUs of each task container and build container (above 0). |
| `LAKANA_RUN__TASK_PIDS` | `256` | With `docker`: the most processes in each task container and build container (16 or more). |

With `LAKANA_RUN__RUNNER=docker`, `LAKANA_RUN__HTTPS_PROXY` is refused at start: builds reach
the package indexes through the egress service. A package index whose certificate a private
certificate authority signed works with both runners once `LAKANA_RUN__CA_BUNDLE` names that
authority's certificate: the worker mounts the trust bundle read-only into each build and task
container (see
[Run Python tasks in containers](../how-to/run-python-tasks-in-containers.md#package-indexes-and-proxies)). The egress service lets a build reach the host and port of each
package index its project uses (port 443 unless the index's URL names another, with a credential
or without); it tunnels https only, so an `http://` index cannot be
reached from a build, and an `http://` `LAKANA_RUN__INDEX_URL` stops a `docker` worker at start
with a message (serve the index over https, or keep `LAKANA_RUN__RUNNER=local`). `docker compose logs egress` names each host and port it refuses
(`Refused a build connection from … to <host>:<port>: not in its token.`). At start a `docker` worker asks the launcher to remove the containers
its own earlier process left (the worker keeps its name for the launcher in `launcher-owner` in its
cache directory, so workers that share a host name never remove each other's tasks); a launcher
that cannot be reached is logged, the worker starts anyway, and each Python task fails with
`runner unavailable: …` until it is reached. A task whose launcher reports no end within its timeout, its
kill grace and 15 minutes fails as `timed out: the launcher reported no end of the task's container …`; the
worker then closes its connection, which makes the launcher remove the container, and
`docker compose logs launcher` has the reason.

`LAKANA_RUN__INDEX_URL`, `LAKANA_RUN__HTTPS_PROXY` and `LAKANA_RUN__NO_PROXY` follow the rules
of the `LAKANA_GIT__*` settings (see [Git network settings](network-settings.md#git-network-settings)); the
index and the proxy may carry a password, and it is redacted from errors, logs and uv's output.
`LAKANA_RUN__CA_BUNDLE` does not: it adds to Mozilla's roots, where `LAKANA_GIT__CA_BUNDLE`
replaces the system store (see
[The trust bundle of builds and tasks](network-settings.md#the-trust-bundle-of-builds-and-tasks)).
Set `LAKANA_RUN__*` where the worker reads them. Related settings are
`LAKANA_RUN__DRAIN_GRACE_SECONDS` and `LAKANA_TASK_TOKENS__MAX_TTL_SECONDS`. The latter caps the
lifetime of every task token and is read by the server and by the worker: set it to the same value
on both. A cap lowered on the server only is not applied to the tokens the worker mints. A task
fails at once if the cap is below its needs (see "Failure, retry and cancel" above).

With `local`, the worker needs `uv` on its `PATH` (the Lakana image ships it); with `docker`, the
task image carries it. The worker logs at startup whether it can run Python assets, and why not. Its cache directory is `$XDG_CACHE_HOME/lakana-worker`, else
`~/.cache/lakana-worker`:

- `venvs/` holds the venvs (at most `LAKANA_RUN__VENV_KEEP`), next to one pair of empty lock
  files (`<key>.lock`, `<key>.build.lock`) per environment ever built there; they are never
  removed by the worker, and you may delete the ones of environments that no longer exist while no
  worker is running. `uv/` holds uv's own download cache. It grows with every package version a project locks, and
  the worker's sweep trims it above `LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES` (10 GiB by default;
  `0` never trims): first what no package refers to any more (`uv cache prune`), then the packages
  downloaded longest ago (`uv cache clean`), never one downloaded within
  `LAKANA_WORKER__PACKAGE_CACHE_MIN_AGE_HOURS` (24), never while the worker builds an environment,
  and not while another worker that shares the cache builds one (it tries again at the next
  sweep). Environments that exist keep working: their files are linked or copied from the cache,
  never symbolic links into it. `lakana env usage` and `lakana worker list` show how large the
  three directories are ([Is my instance healthy?](../how-to/instance-health.md#disk-use)). To
  reclaim all of it at once, stop the worker and delete the `uv/` directory (the next build
  downloads what it needs again). With the local runner, a process that a task detaches from
  its process group is not killed when the task ends; with the docker runner it ends with the
  task's container.
- `attempts/` holds one directory per running attempt. It is removed when the attempt ends. After
  a hard kill of the worker, the leftover is removed by the worker's sweep: at start and then every
  `LAKANA_RUN__SWEEP_INTERVAL` seconds (the same setting paces the artifact store's sweep, and `0`
  turns both off) it deletes directories here that are named like an attempt
  directory (`attempt-` and eight characters), are real directories owned by the worker's user,
  have not changed for longer than a task can run (the asset timeout ceiling, the kill grace,
  60 s and the venv build timeout) and are not locked. A worker holds a lock on its attempt
  directory while the attempt runs (it ends with the worker process), so workers that share a
  cache directory never remove each other's running attempts. The sweep never follows a link and
  leaves anything else in the directory alone, with one log line per name. A directory whose own
  mode user code set to 000 is made accessible again (mode 0700) once it is old enough to sweep,
  then removed like any other. If that fails (for example the cache is on a filesystem that refuses
  the change), it is left in place and a warning names it once per worker start: remove it as the
  worker's user. The sweep runs only between tasks, so one long task delays it.
- Upgrade every worker before you deploy Python assets, and before you deploy an artifact asset
  with a newer platform version: an older worker fails a Python task permanently. A worker that
  knows manifest versions 1 to 4 and meets a newer one refuses it for good with `the project version's
  manifest has version 5, but this worker understands versions 1 to 4; update the worker to the
  platform version that deployed it, or redeploy with this worker's version` (the numbers differ).
  Upgrade the workers first, so that no worker is older than the platform. The order and the way
  back are in [Upgrade to the release with artifacts](../how-to/upgrade-to-artifacts.md).
- The settings for artifacts (`LAKANA_ARTIFACTS__*`) are in
  [`deploy-with-compose.md`](../how-to/deploy-with-compose.md), "Storage for artifacts".
- The Compose bundle's setup is in [`deploy-with-compose.md`](../how-to/deploy-with-compose.md), "Python assets".
